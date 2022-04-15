"""
Convert a trained Keras model to tflite and quantize it.

Copyright (c) 2022 Lindo St. Angel.
"""

from functools import partial
import os
import argparse
import socket
import time

import tensorflow as tf
import numpy as np

from common import load_dataset, WindowGenerator, params_appliance
from logger import log

# Number of samples for post-training quantization calibration.
NUM_CAL = 1000
# Number of standard deviations for sample float32 to int8 conversion.
NUM_STD = 3.0
# Number of inferences to evaluate quantized model.
NUM_EVAL = 1000

rng = np.random.default_rng()

def map_range(value, in_min, in_max, out_min, out_max):
        """Map a value in [in_min, in_max] to [out_min, out_max]."""
        return ((value - in_min) / (in_max - in_min)) * (out_max - out_min) + out_min

def eval_model(model_content, params, dataset, provider):
    """Evaluate tflite model accuracy."""

    log('Starting tflite model evaluation.')

    # Start the tflite interpreter on the tpu and allocate tensors.
    interpreter = tf.lite.Interpreter(model_content=model_content)
    interpreter.allocate_tensors()

    input_details = interpreter.get_input_details()
    log(f'interpreter input details: {input_details}')
    output_details = interpreter.get_output_details()
    log(f'interpreter output details: {output_details}')
    # check the type of the input tensor
    floating_input = input_details[0]['dtype'] == np.float32
    log(f'tflite model floating input: {floating_input}')
    # check the type of the output tensor
    floating_output = output_details[0]['dtype'] == np.float32
    log(f'tflite model floating output: {floating_output}')
    input_index = input_details[0]['index']
    output_index = output_details[0]['index']

    appliance_mean = params[appliance_name]['mean']
    log(f'appliance mean: {appliance_mean}')
    appliance_std = params[appliance_name]['std']
    log(f'appliance std: {appliance_std}')
    appliance_threshold = params[appliance_name]['on_power_threshold']
    log(f'appliance threshold: {appliance_threshold}')

    sample_max = np.amax(dataset[0])
    sample_min = np.amin(dataset[0])
    sample_mean = np.mean(dataset[0])
    sample_std = np.std(dataset[0])
    log(f'dataset sample min: {sample_min}, dataset sample max: {sample_max}')
    log(f'dataset sample mean: {sample_mean}, dataset sample std: {sample_std}')

    # Determine range of samples to preserve dynamic range in int8 conversion.
    in_min = sample_mean - NUM_STD * sample_std
    log(f'in_min: {in_min}')
    in_max = sample_mean + NUM_STD * sample_std
    log(f'in_max: {in_max}')

    # Calculate EVAL_SIZE sized indices of random locations in provider.
    eval_indices = rng.choice(provider.__len__(), size=NUM_EVAL)

    square_of_errors = 0

    start = time.time()
    for i in eval_indices:
        sample, target = provider.__getitem__(i)
        if not sample.any(): continue # skip missing data
        ground_truth = np.squeeze(target) * appliance_std + appliance_mean
        if not floating_input:
            sample = map_range(sample, in_min, in_max, -128.0, 127.0).astype(np.int8)
        interpreter.set_tensor(input_index, sample)
        interpreter.invoke() # run inference
        result = interpreter.get_tensor(output_index)
        prediction = np.squeeze(result)
        if not floating_output:
            prediction = prediction / 127.0
        prediction = prediction * appliance_std + appliance_mean
        if prediction < 0: prediction = 0 # zero out negative energy
        #print(f'sample index: {i} ground_truth: {ground_truth:.3f} prediction: {prediction:.3f}')
        #np.testing.assert_allclose(ground_truth, prediction, rtol=1e-5, atol=0)
        square_of_errors += np.square(ground_truth - prediction)
        # https://stackoverflow.com/questions/56777704/how-to-fix-there-is-at-least-1-reference-to-internal-data-in-the-interpreter-in
        #interpreter = tf.lite.Interpreter(model_content=tflite_model_quant)
        #interpreter.allocate_tensors()
    end = time.time()

    log('Evaluation complete.')
    log(f'Inference rate: {NUM_EVAL / (end - start):.3f} Hz')
    mse = square_of_errors / NUM_EVAL
    log(f'mse: {mse}')

def change_model_batch_shape(model, batch_shape):
    """
    Change input model's batch shape.

    Ref: https://discuss.tensorflow.org/t/change-batch-size-statically-for-inference-tf2/5995
    """
    model_config = model.get_config()
    model_config['layers'][0] = {
        'name': 'new_input',
        'class_name': 'InputLayer',
        'config': {
            'batch_input_shape': batch_shape,
            'dtype': 'float32',
            'sparse': False,
            'name': 'modified_input'
        },
        'inbound_nodes': []
    }
    model_config['layers'][1]['inbound_nodes'] = [[['new_input', 0, 0, {}]]]
    model_config['input_layers'] = [['new_input', 0, 0]]
    new_model = model.__class__.from_config(model_config, custom_objects={})
    new_model.set_weights(model.get_weights())
    return new_model

def representative_dataset_gen(provider, num_cal) -> np.float32:
    """Outputs samples from dataset at random locations."""
    # Get number of samples in provider.
    provider_len = provider.__len__()
    # Calculate num_cal sized indices of random locations.
    indices = rng.choice(provider_len, size=num_cal)
    # Generate samples from provider.
    for i in indices:
        sample, _ = provider.__getitem__(i)
        yield [sample]

def convert(model, provider, num_cal, io_int=False):
    """
    Convert Keras model to tflite, optimize for latency and size,
    quantize weights and activations to int8 and optionally quantize input
    and output layers.

    Returns a quantized tflite model that can be complied for the edge tpu.

    Args:
        model: Keras input model.
        provider: Object that provides samples for calibration.
        num_cal: Number of calibration passes.
        io_int: Boolean indicating int8 input and outputs.

    Returns:
        Quantized tflite model.

    Raises:
        Nothing.
    """
    converter = tf.lite.TFLiteConverter.from_keras_model(model)

    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    # ref_gen must be a callable so use partial to set parameters. 
    ref_gen = partial(representative_dataset_gen,
        provider=provider, num_cal=num_cal)
    converter.representative_dataset = tf.lite.RepresentativeDataset(ref_gen)
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    if io_int:
        converter.inference_input_type = tf.int8
        converter.inference_output_type = tf.int8

    return converter.convert()

def get_arguments():
    parser = argparse.ArgumentParser(
        description='Convert a trained Keras model to tflite and quantize.')
    parser.add_argument('--appliance_name',
                        type=str,
                        default='kettle',
                        help='Name of target appliance.')
    parser.add_argument('--datadir',
                        type=str,
                        default='/home/lindo/Develop/nilm/ml/dataset_management/refit',
                        help='Directory of datasets.')
    parser.add_argument('--save_dir',
                        type=str,
                        default='/home/lindo/Develop/nilm/ml/models',
                        help='Directory to save the quantized model')
    parser.add_argument('--crop',
                        type=int,
                        default=None,
                        help='Number of dataset samples to use. Default uses entire dataset.')
    parser.add_argument('--io_int', action='store_true',
                        help='If set, make tflite inputs and outputs int8.')
    parser.add_argument('--evaluate', action='store_true',
                        help='If set, evaluate tflite model for accuracy.')
    parser.set_defaults(io_int=False)
    parser.set_defaults(evaluate=False)
    return parser.parse_args()

if __name__ == '__main__':
    log(f'Machine name: {socket.gethostname()}')
    args = get_arguments()
    log('Arguments: ')
    log(args)

    # The appliance to train on.
    appliance_name = args.appliance_name

    # Load Keras model from best checkpoint during training.
    model_filepath = os.path.join(args.save_dir, appliance_name)
    log(f'Model file path: {model_filepath}')
    checkpoint_filepath = os.path.join(model_filepath,'checkpoints')
    log(f'Checkpoint file path: {checkpoint_filepath}')

    # Calculate offset parameter from window length.
    window_length = params_appliance[appliance_name]['windowlength']
    offset = int(0.5 * (window_length - 1.0))

    # Load model checkpoint and change batch shape to (1, window_length).
    # This will make the batch size static for use in tpu compilation.
    # The edge tpu compiler accepts only static batch sizes.
    original_model = tf.keras.models.load_model(checkpoint_filepath)
    original_model.summary()
    model = change_model_batch_shape(
        original_model, batch_shape=(1, window_length))
    model.summary()

    # Load dataset.
    dataset_path = os.path.join(
        args.datadir,appliance_name,f'{appliance_name}_training_.csv')
    log(f'dataset: {dataset_path}')
    dataset = load_dataset(dataset_path, args.crop)

    num_train_samples = dataset[0].size
    log(f'There are {num_train_samples/10**6:.3f}M samples in dataset.')

    # Provider of windowed dataset samples and single point targets.
    provider = WindowGenerator(
        dataset=dataset,
        offset=offset,
        batch_size=1,
        shuffle=False)

    # Convert model to tflite and quantize.
    tflite_model_quant = convert(
        model=model,
        provider=provider,
        num_cal=NUM_CAL,
        io_int=args.io_int)

    # Save converted model.
    filepath = os.path.join(args.save_dir, appliance_name,
        f'{appliance_name}_quant.tflite') 
    with open(filepath, 'wb') as file:
        file.write(tflite_model_quant)
    log(f'Quantized tflite model saved to {filepath}.')

    if args.evaluate:
        eval_model(
            model_content=tflite_model_quant,
            params=params_appliance,
            dataset=dataset,
            provider=provider)