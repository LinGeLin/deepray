# Copyright (c) 2021, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""A light weight utilities to train TensorFlow models."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import os
import time
from typing import Optional, Callable, Union

import tensorflow as tf
from absl import logging, flags
from dllogger import Verbosity
from keras.engine import compile_utils
from keras.engine import data_adapter
from packaging import version

if version.parse(tf.keras.__version__.replace("-tf", "+tf")) < version.parse("2.11"):
  from tensorflow.keras import optimizers
else:
  from tensorflow.keras.optimizers import legacy as optimizers
from deepray.core.common import distribution_utils
from deepray.optimizers import optimization
from deepray.optimizers.optimization import GradientAccumulator
from deepray.utils import dllogger_class
from deepray.utils import gpu_affinity
from deepray.utils.flags import common_flags
from deepray.utils.misc import keras_utils
from deepray.utils.benchmark import PerformanceCalculator
from keras import callbacks as callbacks_module
from .module import Module

_SUMMARY_TXT = 'training_summary.txt'
_MIN_SUMMARY_STEPS = 10
FLAGS = flags.FLAGS

if FLAGS.use_dynamic_embedding:
  from tensorflow_recommenders_addons import dynamic_embedding as de
  from tensorflow_recommenders_addons.dynamic_embedding.python.ops.dynamic_embedding_ops import TrainableWrapper, DEResourceVariable
else:
  TrainableWrapper, DEResourceVariable = type(None), type(None)

# Users should always run this script under TF 2.x
# The container haven't changed version number yet, skip the check.
assert tf.version.VERSION.startswith('2.')

gpus = tf.config.experimental.list_physical_devices('GPU')
for gpu in gpus:
  tf.config.experimental.set_memory_growth(gpu, True)

if FLAGS.use_horovod:
  if FLAGS.keras_use_ctl:
    import horovod.tensorflow as hvd
  else:
    import horovod.tensorflow.keras as hvd
  from horovod.tensorflow.compression import Compression

  hvd.init()
  if gpus:
    tf.config.experimental.set_visible_devices(gpus[hvd.local_rank()], 'GPU')
    gpu_affinity.set_affinity(hvd.local_rank())

# Enables XLA in Session Config. Should not be set for TPU.
keras_utils.set_config_v2(FLAGS.enable_xla)

use_float16 = common_flags.use_float16()
if use_float16:
  policy = tf.keras.mixed_precision.Policy("mixed_float16")
  tf.keras.mixed_precision.set_global_policy(policy)
  logging.info("mixed_float16 enabled!")


def write_txt_summary(training_summary, summary_dir):
  """Writes a summary text file to record stats."""
  summary_path = os.path.join(summary_dir, _SUMMARY_TXT)
  with tf.io.gfile.GFile(summary_path, 'wb') as f:
    logging.info('Training Summary: \n%s', str(training_summary))
    f.write(json.dumps(training_summary, indent=4, default=str))


class Trainer(Module):
  """Configures the model for training.

  Example:

  ```python
  model.compile(optimizer=tf.keras.optimizers.Adam(learning_rate=1e-3),
                loss=tf.keras.losses.BinaryCrossentropy(),
                metrics=[tf.keras.metrics.BinaryAccuracy(),
                         tf.keras.metrics.FalseNegatives()])
  ```

  Args:
      optimizer: String (name of optimizer) or optimizer instance. See
        `tf.keras.optimizers`.
      loss: Loss function. May be a string (name of loss function), or
        a `tf.keras.losses.Loss` instance. See `tf.keras.losses`. A loss
        function is any callable with the signature `loss = fn(y_true,
        y_pred)`, where `y_true` are the ground truth values, and
        `y_pred` are the model's predictions.
        `y_true` should have shape
        `(batch_size, d0, .. dN)` (except in the case of
        sparse loss functions such as
        sparse categorical crossentropy which expects integer arrays of
        shape `(batch_size, d0, .. dN-1)`).
        `y_pred` should have shape `(batch_size, d0, .. dN)`.
        The loss function should return a float tensor.
        If a custom `Loss` instance is
        used and reduction is set to `None`, return value has shape
        `(batch_size, d0, .. dN-1)` i.e. per-sample or per-timestep loss
        values; otherwise, it is a scalar. If the model has multiple
        outputs, you can use a different loss on each output by passing a
        dictionary or a list of losses. The loss value that will be
        minimized by the model will then be the sum of all individual
        losses, unless `loss_weights` is specified.
      metrics: List of metrics to be evaluated by the model during
        training and testing. Each of this can be a string (name of a
        built-in function), function or a `tf.keras.metrics.Metric`
        instance. See `tf.keras.metrics`. Typically you will use
        `metrics=['accuracy']`.
        A function is any callable with the signature `result = fn(y_true,
        y_pred)`. To specify different metrics for different outputs of a
        multi-output model, you could also pass a dictionary, such as
        `metrics={'output_a':'accuracy', 'output_b':['accuracy', 'mse']}`.
        You can also pass a list to specify a metric or a list of metrics
        for each output, such as
        `metrics=[['accuracy'], ['accuracy', 'mse']]`
        or `metrics=['accuracy', ['accuracy', 'mse']]`. When you pass the
        strings 'accuracy' or 'acc', we convert this to one of
        `tf.keras.metrics.BinaryAccuracy`,
        `tf.keras.metrics.CategoricalAccuracy`,
        `tf.keras.metrics.SparseCategoricalAccuracy` based on the shapes
        of the targets and of the model output. We do a similar
        conversion for the strings 'crossentropy' and 'ce' as well.
        The metrics passed here are evaluated without sample weighting; if
        you would like sample weighting to apply, you can specify your
        metrics via the `weighted_metrics` argument instead.
      loss_weights: Optional list or dictionary specifying scalar
        coefficients (Python floats) to weight the loss contributions of
        different model outputs. The loss value that will be minimized by
        the model will then be the *weighted sum* of all individual
        losses, weighted by the `loss_weights` coefficients.  If a list,
        it is expected to have a 1:1 mapping to the model's outputs. If a
        dict, it is expected to map output names (strings) to scalar
        coefficients.
      weighted_metrics: List of metrics to be evaluated and weighted by
        `sample_weight` or `class_weight` during training and testing.
      run_eagerly: Bool. Defaults to `False`. If `True`, this `Model`'s
        logic will not be wrapped in a `tf.function`. Recommended to leave
        this as `None` unless your `Model` cannot be run inside a
        `tf.function`. `run_eagerly=True` is not supported when using
        `tf.distribute.experimental.ParameterServerStrategy`.
      steps_per_execution: Int. Defaults to 1. The number of batches to
        run during each `tf.function` call. Running multiple batches
        inside a single `tf.function` call can greatly improve performance
        on TPUs or small models with a large Python overhead. At most, one
        full epoch will be run each execution. If a number larger than the
        size of the epoch is passed, the execution will be truncated to
        the size of the epoch. Note that if `steps_per_execution` is set
        to `N`, `Callback.on_batch_begin` and `Callback.on_batch_end`
        methods will only be called every `N` batches (i.e. before/after
        each `tf.function` execution).
      jit_compile: If `True`, compile the model training step with XLA.
        [XLA](https://www.tensorflow.org/xla) is an optimizing compiler
        for machine learning.
        `jit_compile` is not enabled for by default.
        Note that `jit_compile=True`
        may not necessarily work for all models.
        For more information on supported operations please refer to the
        [XLA documentation](https://www.tensorflow.org/xla).
        Also refer to
        [known XLA issues](https://www.tensorflow.org/xla/known_issues)
        for more details.
      **kwargs: Arguments supported for backwards compatibility only.
  """

  def __init__(
      self,
      model_or_fn: [Callable[..., tf.keras.Model], tf.keras.Model],
      sub_model_or_fn: Optional[Union[Callable[[], tf.keras.Model], tf.keras.Model]] = None,
      optimizer="rmsprop",
      loss=None,
      metrics=None,
      loss_weights=None,
      weighted_metrics=None,
      use_horovod=None,
      run_eagerly=None,
      jit_compile=None,
      **kwargs,
  ):
    self.strategy = distribution_utils.get_distribution_strategy()
    if isinstance(model_or_fn, tf.keras.Model):
      self.model = model_or_fn
    elif callable(model_or_fn):
      self.build_model = model_or_fn
      self.model = model_or_fn()
    else:
      ValueError("Not a reachable model_or_fn.")

    self.sub_model = sub_model_or_fn
    self._loss = loss
    self._metrics = metrics
    self._loss_weights = loss_weights
    self._weighted_metrics = weighted_metrics

    self.use_horovod = use_horovod if use_horovod else FLAGS.use_horovod
    self.run_eagerly = run_eagerly if run_eagerly else FLAGS.run_eagerly
    self.sub_model_export_name = "sub_model"

    self.epochs = FLAGS.epochs

    if not self.use_horovod or hvd.rank() == 0:
      logging.info(" {} Initialize training".format(time.strftime("%Y%m%d %H:%M:%S")))

      logging.info("\ttf.app.flags.FLAGS:")
      for key, value in sorted(FLAGS.flag_values_dict().items()):
        logging.info(f"\t{key:25}= {value}")

    num_train_examples = FLAGS.num_train_examples
    self.global_batch_size = FLAGS.batch_size * FLAGS.num_accumulation_steps
    learning_rate = FLAGS.learning_rate

    if self.use_horovod:
      self.global_batch_size *= hvd.size()
      learning_rate *= hvd.size()

    self.steps_per_epoch = int(num_train_examples / self.global_batch_size)

    if FLAGS.benchmark:
      self.steps_per_epoch = 1000
      self.epochs = 1

    warmup_steps = int(self.epochs * num_train_examples * 0.1 / self.global_batch_size)

    self._performance_calculator = PerformanceCalculator(warmup_steps, self.steps_per_epoch * self.epochs)

    if isinstance(optimizer, optimizers.Optimizer):
      self.optimizer = optimizer
    else:
      self.optimizer = optimization.create_optimizer(
          learning_rate, self.steps_per_epoch * self.epochs, warmup_steps, FLAGS.optimizer_type
      )
    self.use_float16 = common_flags.use_float16()
    if self.use_float16:
      self.optimizer = tf.keras.mixed_precision.LossScaleOptimizer(self.optimizer, dynamic=True)

    self.steps_per_loop = FLAGS.steps_per_summary
    if 1 < self.steps_per_epoch < self.steps_per_loop:
      if not self.use_horovod or hvd.rank() == 0:
        logging.error(
            'steps_per_summary: %d is specified to be greater than '
            ' steps_per_epoch: %d, we will use steps_per_epoch as'
            ' steps_per_summary.', self.steps_per_loop, self.steps_per_epoch
        )
      self.steps_per_loop = self.steps_per_epoch
    assert tf.executing_eagerly()

    if self.run_eagerly:
      # if self.steps_per_loop > 1:
      #   raise ValueError(
      #     'steps_per_loop is used for performance optimization. When you want '
      #     'to run eagerly, you cannot leverage graph mode loop.')
      if isinstance(self.strategy, tf.distribute.experimental.TPUStrategy):
        raise ValueError(
            'TPUStrategy should not run eagerly as it heavily replies on graph'
            ' optimization for the distributed system.'
        )

    if not self.run_eagerly:
      _train_single_step = tf.function(self.train_single_step)
      _train_multi_steps = tf.function(self.train_steps)
      self._test_step = tf.function(self.test_step)
    else:
      _train_single_step = self.train_single_step
      _train_multi_steps = self.train_steps
      self._test_step = self.test_step

    if self.strategy:
      self._train_step = self.train_single_step_strategy
      self._train_steps = self.train_steps_strategy
    else:
      self._train_step = _train_single_step
      self._train_steps = _train_multi_steps

    # Create summary writers
    if not self.use_horovod or hvd.rank() == 0:
      self.summary_dir = os.path.join(FLAGS.model_dir, 'summaries')
      self.eval_summary_writer = tf.summary.create_file_writer(os.path.join(self.summary_dir, 'eval'))
      if self.steps_per_loop >= _MIN_SUMMARY_STEPS:
        # Only writes summary when the stats are collected sufficiently over
        # enough steps.
        self.train_summary_writer = tf.summary.create_file_writer(os.path.join(self.summary_dir, 'train'))
      else:
        self.train_summary_writer = None
    else:
      self.eval_summary_writer = None
      self.train_summary_writer = None
      eval_input_fn = None

    with distribution_utils.get_strategy_scope(self.strategy):
      # To correctly place the model weights on accelerators,
      # model should be created in scope.
      if isinstance(loss, compile_utils.LossesContainer):
        self.loss_container = loss
      else:
        self.loss_container = compile_utils.LossesContainer(loss, loss_weights
                                                            # , output_names=self.output_names
                                                           )
      self.metric_container = compile_utils.MetricsContainer(
          metrics,
          weighted_metrics,
          # output_names=self.output_names,
          # from_serialized=from_serialized,
      ) if metrics or weighted_metrics else None

    if FLAGS.init_checkpoint:
      logging.info(
          'Checkpoint file %s found and restoring from '
          'initial checkpoint for core model.', FLAGS.init_checkpoint
      )
      self.sub_model_checkpoint = tf.train.Checkpoint(model=self.sub_model) if self.sub_model else None
      self.sub_model_checkpoint.restore(FLAGS.init_checkpoint).assert_existing_objects_matched()
      logging.info('Loading from checkpoint file completed')

    self.checkpoint = tf.train.Checkpoint(model=self.model, optimizer=self.optimizer)

    if FLAGS.init_weights:
      logging.info(
          'variables file %s found and restoring from '
          'initial variables for main model.', FLAGS.init_weights
      )
      self.model.load_weights(os.path.join(FLAGS.init_weights, "variables"))
      logging.info('Loading from weights file completed')

    latest_checkpoint_file = tf.train.latest_checkpoint(FLAGS.model_dir)
    if latest_checkpoint_file:
      logging.info('Checkpoint file %s found and restoring from '
                   'checkpoint', latest_checkpoint_file)
      self.checkpoint.restore(latest_checkpoint_file)
      logging.info('Loading from checkpoint file completed')

    self.checkpoint_name = 'ctl_step_{step}.ckpt'
    self.manager = tf.train.CheckpointManager(self.checkpoint, os.path.join(FLAGS.model_dir, 'ckpt'), max_to_keep=3)
    if FLAGS.init_checkpoint:
      self.sub_manager = tf.train.CheckpointManager(
          self.sub_model_checkpoint, os.path.join(FLAGS.model_dir, 'sub_ckpt'), max_to_keep=3
      )
    if FLAGS.num_accumulation_steps > 1:
      self.accum_gradients = GradientAccumulator()

  def fit(
      self,
      train_input=None,
      eval_input=None,
      eval_steps=None,
      verbose="auto",
      callbacks=[],
  ):
    """Trains the model for a fixed number of epochs (dataset iterations).

    Args:
        x: Input data. It could be:
          - A Numpy array (or array-like), or a list of arrays
            (in case the model has multiple inputs).
          - A TensorFlow tensor, or a list of tensors
            (in case the model has multiple inputs).
          - A dict mapping input names to the corresponding array/tensors,
            if the model has named inputs.
          - A `tf.data` dataset. Should return a tuple
            of either `(inputs, targets)` or
            `(inputs, targets, sample_weights)`.
          - A generator or `keras.utils.Sequence` returning `(inputs,
            targets)` or `(inputs, targets, sample_weights)`.
          - A `tf.keras.utils.experimental.DatasetCreator`, which wraps a
            callable that takes a single argument of type
            `tf.distribute.InputContext`, and returns a `tf.data.Dataset`.
            `DatasetCreator` should be used when users prefer to specify the
            per-replica batching and sharding logic for the `Dataset`.
            See `tf.keras.utils.experimental.DatasetCreator` doc for more
            information.
          A more detailed description of unpacking behavior for iterator
          types (Dataset, generator, Sequence) is given below. If these
          include `sample_weights` as a third component, note that sample
          weighting applies to the `weighted_metrics` argument but not the
          `metrics` argument in `compile()`. If using
          `tf.distribute.experimental.ParameterServerStrategy`, only
          `DatasetCreator` type is supported for `x`.
        y: Target data. Like the input data `x`,
          it could be either Numpy array(s) or TensorFlow tensor(s).
          It should be consistent with `x` (you cannot have Numpy inputs and
          tensor targets, or inversely). If `x` is a dataset, generator,
          or `keras.utils.Sequence` instance, `y` should
          not be specified (since targets will be obtained from `x`).
        batch_size: Integer or `None`.
            Number of samples per gradient update.
            If unspecified, `batch_size` will default to 32.
            Do not specify the `batch_size` if your data is in the
            form of datasets, generators, or `keras.utils.Sequence`
            instances (since they generate batches).
        epochs: Integer. Number of epochs to train the model.
            An epoch is an iteration over the entire `x` and `y`
            data provided
            (unless the `steps_per_epoch` flag is set to
            something other than None).
            Note that in conjunction with `initial_epoch`,
            `epochs` is to be understood as "final epoch".
            The model is not trained for a number of iterations
            given by `epochs`, but merely until the epoch
            of index `epochs` is reached.
        verbose: 'auto', 0, 1, or 2. Verbosity mode.
            0 = silent, 1 = progress bar, 2 = one line per epoch.
            'auto' defaults to 1 for most cases, but 2 when used with
            `ParameterServerStrategy`. Note that the progress bar is not
            particularly useful when logged to a file, so verbose=2 is
            recommended when not running interactively (eg, in a production
            environment).
        callbacks: List of `keras.callbacks.Callback` instances.
            List of callbacks to apply during training.
            See `tf.keras.callbacks`. Note
            `tf.keras.callbacks.ProgbarLogger` and
            `tf.keras.callbacks.History` callbacks are created automatically
            and need not be passed into `model.fit`.
            `tf.keras.callbacks.ProgbarLogger` is created or not based on
            `verbose` argument to `model.fit`.
            Callbacks with batch-level calls are currently unsupported with
            `tf.distribute.experimental.ParameterServerStrategy`, and users
            are advised to implement epoch-level calls instead with an
            appropriate `steps_per_epoch` value.
        validation_split: Float between 0 and 1.
            Fraction of the training data to be used as validation data.
            The model will set apart this fraction of the training data,
            will not train on it, and will evaluate
            the loss and any model metrics
            on this data at the end of each epoch.
            The validation data is selected from the last samples
            in the `x` and `y` data provided, before shuffling. This
            argument is not supported when `x` is a dataset, generator or
            `keras.utils.Sequence` instance.
            If both `validation_data` and `validation_split` are provided,
            `validation_data` will override `validation_split`.
            `validation_split` is not yet supported with
            `tf.distribute.experimental.ParameterServerStrategy`.
        validation_data: Data on which to evaluate
            the loss and any model metrics at the end of each epoch.
            The model will not be trained on this data. Thus, note the fact
            that the validation loss of data provided using
            `validation_split` or `validation_data` is not affected by
            regularization layers like noise and dropout.
            `validation_data` will override `validation_split`.
            `validation_data` could be:
              - A tuple `(x_val, y_val)` of Numpy arrays or tensors.
              - A tuple `(x_val, y_val, val_sample_weights)` of NumPy
                arrays.
              - A `tf.data.Dataset`.
              - A Python generator or `keras.utils.Sequence` returning
              `(inputs, targets)` or `(inputs, targets, sample_weights)`.
            `validation_data` is not yet supported with
            `tf.distribute.experimental.ParameterServerStrategy`.
        shuffle: Boolean (whether to shuffle the training data
            before each epoch) or str (for 'batch'). This argument is
            ignored when `x` is a generator or an object of tf.data.Dataset.
            'batch' is a special option for dealing
            with the limitations of HDF5 data; it shuffles in batch-sized
            chunks. Has no effect when `steps_per_epoch` is not `None`.
        class_weight: Optional dictionary mapping class indices (integers)
            to a weight (float) value, used for weighting the loss function
            (during training only).
            This can be useful to tell the model to
            "pay more attention" to samples from
            an under-represented class.
        sample_weight: Optional Numpy array of weights for
            the training samples, used for weighting the loss function
            (during training only). You can either pass a flat (1D)
            Numpy array with the same length as the input samples
            (1:1 mapping between weights and samples),
            or in the case of temporal data,
            you can pass a 2D array with shape
            `(samples, sequence_length)`,
            to apply a different weight to every timestep of every sample.
            This argument is not supported when `x` is a dataset, generator,
            or `keras.utils.Sequence` instance, instead provide the
            sample_weights as the third element of `x`.
            Note that sample weighting does not apply to metrics specified
            via the `metrics` argument in `compile()`. To apply sample
            weighting to your metrics, you can specify them via the
            `weighted_metrics` in `compile()` instead.
        initial_epoch: Integer.
            Epoch at which to start training
            (useful for resuming a previous training run).
        steps_per_epoch: Integer or `None`.
            Total number of steps (batches of samples)
            before declaring one epoch finished and starting the
            next epoch. When training with input tensors such as
            TensorFlow data tensors, the default `None` is equal to
            the number of samples in your dataset divided by
            the batch size, or 1 if that cannot be determined. If x is a
            `tf.data` dataset, and 'steps_per_epoch'
            is None, the epoch will run until the input dataset is
            exhausted.  When passing an infinitely repeating dataset, you
            must specify the `steps_per_epoch` argument. If
            `steps_per_epoch=-1` the training will run indefinitely with an
            infinitely repeating dataset.  This argument is not supported
            with array inputs.
            When using `tf.distribute.experimental.ParameterServerStrategy`:
              * `steps_per_epoch=None` is not supported.
        validation_steps: Only relevant if `validation_data` is provided and
            is a `tf.data` dataset. Total number of steps (batches of
            samples) to draw before stopping when performing validation
            at the end of every epoch. If 'validation_steps' is None,
            validation will run until the `validation_data` dataset is
            exhausted. In the case of an infinitely repeated dataset, it
            will run into an infinite loop. If 'validation_steps' is
            specified and only part of the dataset will be consumed, the
            evaluation will start from the beginning of the dataset at each
            epoch. This ensures that the same validation samples are used
            every time.
        validation_batch_size: Integer or `None`.
            Number of samples per validation batch.
            If unspecified, will default to `batch_size`.
            Do not specify the `validation_batch_size` if your data is in
            the form of datasets, generators, or `keras.utils.Sequence`
            instances (since they generate batches).
        validation_freq: Only relevant if validation data is provided.
          Integer or `collections.abc.Container` instance (e.g. list, tuple,
          etc.).  If an integer, specifies how many training epochs to run
          before a new validation run is performed, e.g. `validation_freq=2`
          runs validation every 2 epochs. If a Container, specifies the
          epochs on which to run validation, e.g.
          `validation_freq=[1, 2, 10]` runs validation at the end of the
          1st, 2nd, and 10th epochs.
        max_queue_size: Integer. Used for generator or
          `keras.utils.Sequence` input only. Maximum size for the generator
          queue.  If unspecified, `max_queue_size` will default to 10.
        workers: Integer. Used for generator or `keras.utils.Sequence` input
            only. Maximum number of processes to spin up
            when using process-based threading. If unspecified, `workers`
            will default to 1.
        use_multiprocessing: Boolean. Used for generator or
            `keras.utils.Sequence` input only. If `True`, use process-based
            threading. If unspecified, `use_multiprocessing` will default to
            `False`. Note that because this implementation relies on
            multiprocessing, you should not pass non-picklable arguments to
            the generator as they can't be passed easily to children
            processes.

    Unpacking behavior for iterator-like inputs:
        A common pattern is to pass a tf.data.Dataset, generator, or
      tf.keras.utils.Sequence to the `x` argument of fit, which will in fact
      yield not only features (x) but optionally targets (y) and sample
      weights.  Keras requires that the output of such iterator-likes be
      unambiguous. The iterator should return a tuple of length 1, 2, or 3,
      where the optional second and third elements will be used for y and
      sample_weight respectively. Any other type provided will be wrapped in
      a length one tuple, effectively treating everything as 'x'. When
      yielding dicts, they should still adhere to the top-level tuple
      structure.
      e.g. `({"x0": x0, "x1": x1}, y)`. Keras will not attempt to separate
      features, targets, and weights from the keys of a single dict.
        A notable unsupported data type is the namedtuple. The reason is
      that it behaves like both an ordered datatype (tuple) and a mapping
      datatype (dict). So given a namedtuple of the form:
          `namedtuple("example_tuple", ["y", "x"])`
      it is ambiguous whether to reverse the order of the elements when
      interpreting the value. Even worse is a tuple of the form:
          `namedtuple("other_tuple", ["x", "y", "z"])`
      where it is unclear if the tuple was intended to be unpacked into x,
      y, and sample_weight or passed through as a single element to `x`. As
      a result the data processing code will simply raise a ValueError if it
      encounters a namedtuple. (Along with instructions to remedy the
      issue.)

    Returns:
        A `History` object. Its `History.history` attribute is
        a record of training loss values and metrics values
        at successive epochs, as well as validation loss values
        and validation metrics values (if applicable).

    Raises:
        RuntimeError: 1. If the model was never compiled or,
        2. If `model.fit` is  wrapped in `tf.function`.

        ValueError: In case of mismatch between the provided input data
            and what the model expects or when the input data is empty.
    """
    if FLAGS.keras_use_ctl:
      verbose = 0  # training_module._get_verbosity(verbose, self.strategy)

      # Container that configures and calls `tf.keras.Callback`s.
      if not isinstance(callbacks, callbacks_module.CallbackList):
        # self.callbacks = callbacks if callbacks else []
        self.callbacks = callbacks_module.CallbackList(
            callbacks,
            add_history=True,
            add_progbar=verbose != 0,
            model=self.model,
            verbose=verbose,
            epochs=self.epochs,
            steps=self.steps_per_epoch * self.epochs,
        )
      return self.run_customized_training_loop(train_input, eval_input)
    else:
      self.model.compile(
          optimizer=self.optimizer,
          loss=self._loss,
          loss_weights=self._loss_weights,
          metrics=self._metrics,
          weighted_metrics=self._weighted_metrics,
          run_eagerly=self.run_eagerly
      )

      if not FLAGS.benchmark:
        # Create Tensorboard summary and checkpoint callbacks.
        summary_dir = os.path.join(FLAGS.model_dir, "summaries")
        callbacks.append(tf.keras.callbacks.TensorBoard(summary_dir, profile_batch=0))

        # Horovod: save checkpoints only on worker 0 to prevent other workers from corrupting them.
        if not self.use_horovod or hvd.rank() == 0:
          checkpoint_path = os.path.join(FLAGS.model_dir, "checkpoint")
          callbacks.append(tf.keras.callbacks.ModelCheckpoint(checkpoint_path, save_weights_only=True))
      if FLAGS.use_horovod:
        callbacks += [
            # Horovod: broadcast initial variable states from rank 0 to all other processes.
            # This is necessary to ensure consistent initialization of all workers when
            # training is started with random weights or restored from a checkpoint.
            # hvd callback用于广播rank0的初始化器产生的值
            de.keras.callbacks.DEHvdBroadcastGlobalVariablesCallback(root_rank=0)
            if FLAGS.use_dynamic_embedding else hvd.callbacks.BroadcastGlobalVariablesCallback(0),
        ]

      # Horovod: write logs on worker 0.
      verbose = 2 if not self.use_horovod or hvd.rank() == 0 else 0
      history = self.model.fit(
          train_input,
          epochs=self.epochs,
          steps_per_epoch=self.steps_per_epoch,
          callbacks=callbacks,
          validation_data=eval_input,
          validation_steps=eval_steps,
          verbose=verbose
      )
      return history

  def run_customized_training_loop(
      self,
      train_input=None,
      eval_input=None,
  ):
    if self.epochs > 1 and FLAGS.num_train_examples == -1:
      raise ValueError('When the num_train_examples is INFINITE or UNKNOWN, we just can run one epoch.')

    self._performance_calculator.init()

    # To reduce unnecessary send/receive input pipeline operation, we place input
    # pipeline ops in worker task.
    train_iterator = distribution_utils.make_distributed_iterator(self.strategy, train_input)

    # Training loop starts here.
    self.current_step = self._first_steps = self.optimizer.iterations.numpy()

    self.first_batch = True
    if not hasattr(self.model, 'optimizer'):
      raise ValueError('User should set optimizer attribute to model '
                       'inside `model_fn`.')
    # if self.sub_model_export_name and self.sub_model is None:
    #   raise ValueError('sub_model_export_name is specified as %s, but '
    #                    'sub_model is None.' % self.sub_model_export_name)

    self._steps_from_save = 0
    start_time = time.time()
    self._perf_wo = 0
    self._perf_wo_n = 0

    self.on_train_begin()
    for epoch in range(self.epochs):
      self.on_epoch_begin(epoch)
      while self.steps_per_epoch <= 0 or self._step_epoch < self.steps_per_epoch:
        t0 = time.time()
        self.on_batch_begin(self.current_step)
        # Runs several steps in the host while loop.
        steps, num_accumulation_steps = self.steps_to_run(self.current_step, self.steps_per_epoch, self.steps_per_loop)

        try:
          if steps == 1:
            # TODO(zongweiz): merge with train_steps once tf.while_loop
            # GPU performance bugs are fixed.
            tmp_logs = self._train_step(next(train_iterator), num_accumulation_steps)
          else:
            # Converts steps to a Tensor to avoid tf.function retracing.
            tmp_logs = self._train_steps(
                train_iterator, tf.convert_to_tensor(steps, dtype=tf.int32), num_accumulation_steps
            )
        except (tf.errors.OutOfRangeError, StopIteration):
          logging.info(f"Done reading data for epoch {epoch}")
          if self.optimizer.iterations.numpy() == self._first_steps:
            logging.warning("No data was processed.")
            return None
          elif steps > 1 and self.optimizer.iterations.numpy() > self.current_step:
            steps = self.optimizer.iterations.numpy() - self.current_step
            tmp_logs = self.get_metrics_result()
            self.first_batch = False
            self.on_batch_end(tmp_logs, steps, t0)
          break

        self.first_batch = False
        self.on_batch_end(tmp_logs, steps, t0)
      self.on_epoch_end(epoch, self.current_step, eval_input)
    self.on_train_end()

    total_time = time.time() - start_time
    results_perf = self._performance_calculator.results
    if not self._performance_calculator.completed:
      logging.info(f"self._performance_calculator.completed: {self._performance_calculator.completed}")
      results_perf = self._performance_calculator.get_current_benchmark_results()

    # self._save_checkpoint(self.manager, self.current_step)
    # self.save_model_to_export()
    # self.save_model_to_pb()
    if not self.use_horovod or hvd.rank() == 0:
      # if FLAGS.use_dynamic_embedding:
      #   self.save_model_to_serving()

      if self.sub_model:
        self._save_checkpoint(self.sub_manager)

      if eval_input:
        logging.info('Running final evaluation after training is complete.')
        self.run_evaluation(eval_input, self.current_step)
      training_summary = {
          'total_training_steps': self.current_step,
          'train_loss': self._float_metric_value(self.loss_container.metrics[0]),
      }
      if self.metric_container and self.metric_container.metrics:
        # TODO(hongkuny): Cleans up summary reporting in text.
        for metric in self.metric_container.metrics:
          training_summary['last_' + metric.name] = self._float_metric_value(metric)
          # training_summary['eval_metrics'] = _float_metric_value(self.metric_container.metrics[0])

      write_txt_summary(training_summary, self.summary_dir)

      dllogging = dllogger_class.dllogger_class(FLAGS.dllog_path)
      total_sentences = self.current_step * self.global_batch_size
      logging.info("-----------------------------")
      logging.info("  Batch size = %d", FLAGS.batch_size)
      logging.info("  Num steps = %d", self.current_step)
      logging.info("  LR = %g", FLAGS.learning_rate)
      if self.use_horovod:
        logging.info("Multi-GPU training with TF Horovod")
        logging.info("hvd.size() = %d", hvd.size())
      logging.info("Total Training Time = %0.2f for Examples = %d", total_time, total_sentences)
      logging.info("Throughput Average (examples/sec) with overhead = %0.2f", results_perf['throughput'])
      if self._perf_wo_n != 0:
        logging.info("Throughput Average (examples/sec) = %0.2f", self._perf_wo / self._perf_wo_n)
      logging.info("-----------------------------")

      if dllogging and self._perf_wo_n != 0:
        dllogging.logger.log(
            step=(), data={"throughput_train": self._perf_wo / self._perf_wo_n}, verbosity=Verbosity.DEFAULT
        )
        dllogging.logger.log(step=(), data={"total_loss": training_summary['train_loss']}, verbosity=Verbosity.DEFAULT)
        dllogging.logger.log(data=results_perf, step=tuple())

      return self.model

  def train_single_step(self, iterator, num_grad_accumulates):
    """Performs a distributed training step.

    Args:
      iterator: the distributed iterator of training datasets.

    Raises:
      ValueError: Any of the arguments or tensor shapes are invalid.
    """
    if num_grad_accumulates != 1:
      for _ in tf.range(num_grad_accumulates):
        self.forward(iterator)
        if _ == 0 or (_ + 1) % num_grad_accumulates == 0:
          self.step(num_grad_accumulates)
        if self.use_horovod and _ == 0 and self.first_batch:
          hvd.broadcast_variables(self.model.variables, 0)
          hvd.broadcast_variables(self.optimizer.variables(), 0)
    else:
      self._replicated_step(iterator, self.first_batch)
    return self.get_metrics_result()

  @property
  def trainable_variables(self):
    if hasattr(self.loss_container, 'trainable_variables'):
      return self.model.trainable_variables + self.loss_container.trainable_variables
    else:
      return self.model.trainable_variables

  def _replicated_step(self, inputs, first_batch=False):
    """Replicated training step."""
    inputs, labels, sample_weight = data_adapter.unpack_x_y_sample_weight(inputs)
    with tf.GradientTape() as tape:
      model_outputs = self.model(inputs, training=True)
      loss = self.loss_container(labels, model_outputs, sample_weight=sample_weight)

    if self.use_horovod and not FLAGS.use_dynamic_embedding:
      tape = hvd.DistributedGradientTape(
          tape, sparse_as_dense=True, compression=Compression.fp16 if self.use_float16 else Compression.none
      )
    # Run backwards pass.
    self.optimizer.minimize(loss, self.trainable_variables, tape=tape)

    if self.use_horovod and first_batch:
      broadcast_vars = [
          var for var in self.model.variables
          if (not isinstance(var, TrainableWrapper)) and (not isinstance(var, DEResourceVariable))
      ]
      hvd.broadcast_variables(broadcast_vars, root_rank=0)

      opt_broadcast_vars = [
          var for var in self.optimizer.variables()
          if (not isinstance(var, TrainableWrapper)) and (not isinstance(var, DEResourceVariable))
      ]

      hvd.broadcast_variables(opt_broadcast_vars, root_rank=0)

    # For reporting, the metric takes the mean of losses.
    if self.metric_container:
      self.metric_container.update_state(y_true=labels, y_pred=model_outputs, sample_weight=sample_weight)

  def forward(self, inputs):
    inputs, labels, sample_weight = data_adapter.unpack_x_y_sample_weight(inputs)
    with tf.GradientTape() as tape:
      model_outputs = self.model(inputs, training=True)
      loss = self.loss_container(labels, model_outputs, sample_weight=sample_weight)

    # Compute gradients
    if version.parse(tf.keras.__version__.replace("-tf", "+tf")) < version.parse("2.11"):
      grads_and_vars = self.optimizer._compute_gradients(loss=loss, var_list=self.trainable_variables, tape=tape)
    else:
      grads_and_vars = self.optimizer.compute_gradients(loss=loss, var_list=self.trainable_variables, tape=tape)
    grads = [g for g, _ in grads_and_vars]
    self.accum_gradients.add_gradients(grads)

    # For reporting, the metric takes the mean of losses.
    if self.metric_container:
      self.metric_container.update_state(y_true=labels, y_pred=model_outputs, sample_weight=sample_weight)

  def step(self, num_grad_accumulates):
    gradients = self.accum_gradients.gradients
    if self.use_horovod:
      gradients = [
          None if g is None else hvd.allreduce(
              g / tf.cast(num_grad_accumulates, g.dtype),
              compression=Compression.fp16 if self.use_float16 else Compression.none
          ) for g in gradients
      ]
    else:
      gradients = [None if g is None else g / tf.cast(num_grad_accumulates, g.dtype) for g in gradients]

    self.optimizer.apply_gradients(zip(gradients, self.trainable_variables))
    self.accum_gradients.reset()

  def test_step(self, iterator):
    """Calculates evaluation metrics on distributed devices."""

    def _test_step_fn(inputs):
      """Replicated accuracy calculation."""
      inputs, labels, sample_weight = data_adapter.unpack_x_y_sample_weight(inputs)
      model_outputs = self.model(inputs, training=False)
      loss = self.loss_container(labels, model_outputs, sample_weight=sample_weight)
      if self.metric_container:
        self.metric_container.update_state(labels, model_outputs, sample_weight=sample_weight)

    if self.strategy:
      self.strategy.run(_test_step_fn, args=(next(iterator),))
    else:
      _test_step_fn(next(iterator))

  def train_steps_strategy(self, iterator, steps, num_grad_accumulates):
    """Performs distributed training steps in a loop.

    Args:
      iterator: the distributed iterator of training datasets.
      steps: an tf.int32 integer tensor to specify number of steps to run
        inside host training loop.

    Raises:
      ValueError: Any of the arguments or tensor shapes are invalid.
    """
    if not isinstance(steps, tf.Tensor):
      raise ValueError('steps should be an Tensor. Python object may cause '
                       'retracing.')

    if num_grad_accumulates != 1:
      for _ in tf.range(steps * num_grad_accumulates):
        self.strategy.run(self.forward, args=(next(iterator),))
        if _ == 0 or (_ + 1) % num_grad_accumulates == 0:
          self.strategy.run(self.step, args=(num_grad_accumulates,))
    else:
      for _ in tf.range(steps):
        self.strategy.run(self._replicated_step, args=(next(iterator),))
    return self.get_metrics_result()

  def train_steps(self, iterator, steps, num_grad_accumulates):
    if not isinstance(steps, tf.Tensor):
      raise ValueError('steps should be an Tensor. Python object may cause '
                       'retracing.')

    if num_grad_accumulates != 1:
      for _ in tf.range(steps * num_grad_accumulates):
        self.forward(next(iterator))
        if _ == 0 or (_ + 1) % num_grad_accumulates == 0:
          self.step(num_grad_accumulates)
        if self.use_horovod and _ == 0 and self.first_batch:
          hvd.broadcast_variables(self.model.variables, 0)
          hvd.broadcast_variables(self.optimizer.variables(), 0)
    else:
      for _ in tf.range(steps):
        self._replicated_step(next(iterator), (self.first_batch and _ == 0))
    return self.get_metrics_result()

  def train_single_step_strategy(self, iterator, num_grad_accumulates):
    """Performs a distributed training step.

    Args:
      iterator: the distributed iterator of training datasets.

    Raises:
      ValueError: Any of the arguments or tensor shapes are invalid.
    """
    if num_grad_accumulates != 1:
      for _ in tf.range(num_grad_accumulates):
        self.strategy.run(self.forward, args=(iterator,))
        if _ == 0 or (_ + 1) % num_grad_accumulates == 0:
          self.strategy.run(self.step, args=(num_grad_accumulates,))
    else:
      self.strategy.run(self._replicated_step, args=(iterator,))
    return self.get_metrics_result()

  def export_tfra(self):
    if not self.use_horovod or hvd.rank() == 0:
      save_options = tf.saved_model.SaveOptions(namespace_whitelist=['TFRA'])
      tf.saved_model.save(self.model, "test_tfra", options=save_options)
