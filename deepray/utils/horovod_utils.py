# Copyright (c) 2020 NVIDIA CORPORATION. All rights reserved.
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

import horovod.tensorflow.keras as hvd
from absl import logging, flags

FLAGS = flags.FLAGS


def get_rank():
  try:
    return hvd.rank()
  except:
    return 0


def get_world_size():
  try:
    return hvd.size()
  except:
    return 1


def is_main_process():
  return not FLAGS.use_horovod or get_rank() == 0
