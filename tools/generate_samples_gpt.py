# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

"""Sample Generate GPT"""

import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__),
                                             os.path.pardir)))

from megatron import get_args
from megatron import print_rank_0
from megatron import get_tokenizer
from megatron import mpu
from megatron.checkpointing import load_checkpoint
from megatron.initialize import initialize_megatron
from megatron.model import GPTModel
from megatron.training import get_model
from megatron.text_generation_utils import generate_and_write_samples_unconditional
from megatron.text_generation_utils import generate_samples_input_from_file
from megatron.text_generation_utils import generate_samples_interactive
import deepspeed

def model_provider(pre_process=True, post_process=True):
    """Build the model."""

    print_rank_0('building GPT model ...')
    model = GPTModel(num_tokentypes=0, parallel_output=False,
                     pre_process=pre_process, post_process=post_process)

    print(f'model = {model}')
    return model


def add_text_generate_args(parser):
    """Text generation arguments."""
    group = parser.add_argument_group(title='text generation')

    group.add_argument("--temperature", type=float, default=1.0,
                       help='Sampling temperature.')
    group.add_argument("--greedy", action='store_true', default=False,
                       help='Use greedy sampling.')
    group.add_argument("--top_p", type=float, default=0.0,
                       help='Top p sampling.')
    group.add_argument("--top_k", type=int, default=0,
                       help='Top k sampling.')
    group.add_argument("--out-seq-length", type=int, default=1024,
                       help='Size of the output generated text.')
    group.add_argument("--sample-input-file", type=str, default=None,
                       help='Get input from file instead of interactive mode, '
                       'each line is an input.')
    group.add_argument("--sample-output-file", type=str, default=None,
                       help='Output file got from --sample-input-file')
    group.add_argument("--num-samples", type=int, default=0,
                       help='Number of samples to generate unconditionally, '
                       'defaults to 0 and interactive conditional sampling')
    group.add_argument("--genfile", type=str,
                       help='Output file when generating unconditionally')
    group.add_argument("--recompute", action='store_true',
                       help='During generation recompute all attention '
                       'instead of using previously computed keys/values.')

    return parser


def main():
    """Main program."""

    initialize_megatron(extra_args_provider=add_text_generate_args,
                        args_defaults={'tokenizer_type': 'GPT2BPETokenizer',
                                       'no_load_rng': True,
                                       'no_load_optim': True})

    args = get_args()
    if args.num_layers_per_virtual_pipeline_stage is not None:
        print("Interleaved pipeline schedule is not yet supported for text generation.")
        exit()
    
    import torch
    deepspeed.utils.groups.initialize(ep_size=torch.distributed.get_world_size())

    # Set up model and load checkpoint.
    model = get_model(model_provider)

    #if args.load is not None:
    #    _ = load_checkpoint(model, None, None)

    assert len(model) == 1, "Above condition should have caught this"
    model = model[0]

    if args.ds_inference:
        model = ds_inference(model, args)
        print('> DeepSpeed Inference initialized')

    # Generate samples.
    if args.num_samples == 0:
        args.micro_batch_size = 1
        if args.sample_input_file != None:
            generate_samples_input_from_file(model)
        else:
            generate_samples_interactive(model)
    else:
        generate_and_write_samples_unconditional(model)

def ds_inference(model, args):
    m = None
    simple = True

    if simple:
        import deepspeed.module_inject as module_inject
        import megatron.model as mm
        import torch
        engine = deepspeed.init_inference(model=model,
                                          mp_size=args.tensor_model_parallel_size, 
                                          mpu=mpu,
                                          dtype=torch.half,
                                          return_tuple=False,
                                          replace_with_kernel_inject=True,
                                          injection_policy={mm.transformer.ParallelTransformerLayer:module_inject.replace_policy.MegatronLayerPolicy})
                                          #replace_method='auto')
        m = engine.module
    else:

        import deepspeed.module_inject as module_inject
        policy = module_inject.replace_policy.MegatronLayerPolicy
        policy.version = 1
        import torch
        q_scale = [[torch.randn(2).to(torch.cuda.current_device()) / 100 for _ in range(4)] for _ in range(96)]
        quantize_settings = (q_scale, #quantization_scales
                 1, #merge_count
                 False, #mlp_extra_grouping
                 1, #quantize_groups
                )

        import megatron.model as mm
        m = module_inject.replace_transformer_layer(orig_layer_impl=mm.transformer.ParallelTransformerLayer,
                                  model=model,
                                  policy=policy,
                                  hidden_size=args.hidden_size,
                                  num_attention_heads=args.num_attention_heads,
                                  mp_size=args.tensor_model_parallel_size,
                                  mp_group=mpu.get_tensor_model_parallel_group(),
                                  #dp_group=mpu.get_data_parallel_group, 
                                  fp16=args.fp16,
                                  training=False,
                                  quantize=True,
                                  quantize_settings=quantize_settings)
    return m

if __name__ == "__main__":

    main()
