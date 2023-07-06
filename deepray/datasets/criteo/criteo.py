# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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
"""Criteo dataset."""

import sys

from absl import flags

from deepray.datasets.datapipeline import DataPipeLine

FLAGS = flags.FLAGS
FLAGS([
    sys.argv[0],
    "--num_train_examples=11932672",
])


class Criteo(DataPipeLine):

  def build_dataset(self, input_file_pattern, batch_size, is_training=True, prebatch_size=0, *args, **kwargs):
    pass
