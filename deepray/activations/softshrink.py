# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
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

import tensorflow as tf
from deepray.utils.types import Number, TensorLike


@tf.keras.utils.register_keras_serializable(package="Deepray")
def softshrink(x: TensorLike, lower: Number = -0.5, upper: Number = 0.5) -> tf.Tensor:
    r"""Soft shrink function.

    Computes soft shrink function:

    $$
    \mathrm{softshrink}(x) =
    \begin{cases}
        x - \mathrm{lower} & \text{if } x < \mathrm{lower} \\
        x - \mathrm{upper} & \text{if } x > \mathrm{upper} \\
        0                  & \text{otherwise}
    \end{cases}.
    $$

    Usage:

    >>> x = tf.constant([-1.0, 0.0, 1.0])
    >>> dp.activations.softshrink(x)
    <tf.Tensor: shape=(3,), dtype=float32, numpy=array([-0.5,  0. ,  0.5], dtype=float32)>

    Args:
        x: A `Tensor`. Must be one of the following types:
            `bfloat16`, `float16`, `float32`, `float64`.
        lower: `float`, lower bound for setting values to zeros.
        upper: `float`, upper bound for setting values to zeros.
    Returns:
        A `Tensor`. Has the same type as `x`.
    """
    if lower > upper:
        raise ValueError(
            "The value of lower is {} and should"
            " not be higher than the value "
            "variable upper, which is {} .".format(lower, upper)
        )
    x = tf.convert_to_tensor(x)
    values_below_lower = tf.where(x < lower, x - lower, 0)
    values_above_upper = tf.where(upper < x, x - upper, 0)
    return values_below_lower + values_above_upper
