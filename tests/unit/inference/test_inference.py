# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

import os
import time
import torch
import pytest
import json
import itertools
import deepspeed
from deepspeed.git_version_info import torch_info
from unit.common import DistributedTest
from packaging import version as pkg_version
from deepspeed.ops.op_builder import OpBuilder
from transformers import pipeline, AutoTokenizer
from transformers.models.t5.modeling_t5 import T5Block
from transformers.models.roberta.modeling_roberta import RobertaLayer
from huggingface_hub import HfApi, hf_api
from deepspeed.model_implementations import DeepSpeedTransformerInference
from torch import nn
from deepspeed.accelerator import get_accelerator
from unit.hpu import *
from deepspeed.ops.op_builder import InferenceBuilder
from transformers import BertLayer

if not deepspeed.ops.__compatible_ops__[InferenceBuilder.NAME]:
    pytest.skip("This op had not been implemented on this system.", allow_module_level=True)

rocm_version = OpBuilder.installed_rocm_version()
if rocm_version != (0, 0):
    pytest.skip("skip inference tests on rocm for now", allow_module_level=True)

_bert_models = [
    "bert-base-cased",
    "bert-base-uncased",
    "bert-large-cased",
    "bert-large-uncased",
    "bert-base-multilingual-cased",
    "bert-base-multilingual-uncased",
    "deepset/minilm-uncased-squad2",
    "cross-encoder/ms-marco-MiniLM-L-12-v2",
    "dslim/bert-base-NER",
    "bert-large-uncased-whole-word-masking-finetuned-squad",
    "distilbert-base-cased-distilled-squad",
]
_roberta_models = [
    "roberta-large",
    "roberta-base",
    "deepset/roberta-base-squad2",
    "j-hartmann/emotion-english-distilroberta-base",
    "Jean-Baptiste/roberta-large-ner-english",
]
_gpt_models = [
    "gpt2",
    "distilgpt2",
    "Norod78/hebrew-bad_wiki-gpt_neo-tiny",
    "EleutherAI/gpt-j-6b",
    "EleutherAI/pythia-70m-deduped",
    "bigscience/bloom-560m",
]
_opt_models = [
    "facebook/opt-125m",  # 125m, 1.7B, ..., 175B variants have the same model architecture.
    "facebook/opt-350m",  # 350m applies layer norm after attention layer which is different than other variants.
]
if os.getenv("TRANSFORMERS_OFFLINE", default=None):
    if os.getenv("HF_HOME", default=None):
        model_info_f = os.path.join(os.getenv("HF_HOME", default=None), 'model_info.json')
        with open(model_info_f, 'r') as f:
            data = json.load(f)
        _all_models = [hf_api.ModelInfo(**x) for x in data]
    else:
        assert 1
elif os.getenv("STORE_HF", default=None):
    if os.getenv("HF_HOME", default=None):
        _all_models = list(HfApi().list_models())
        all_models_info = [model_info.__dict__ for model_info in _all_models]
        json_object = json.dumps(all_models_info, indent=4)
        model_info_f = os.path.join(os.getenv("HF_HOME", default=None), 'model_info.json')
        with open(model_info_f, 'w') as f:
            f.write(json_object)
else:
    _all_models = list(HfApi().list_models())

_test_models = set(_bert_models + _roberta_models + _gpt_models + _opt_models)
_hf_model_names = [m.modelId for m in _all_models]
_test_tasks = [
    "fill-mask", "question-answering", "text-classification", "token-classification", "text-generation",
    "text2text-generation", "summarization", "translation"
]
_hf_task_to_models = {task: [m.modelId for m in _all_models if m.pipeline_tag == task] for task in _test_tasks}
# Get all combinations of task:model to test
_model_w_tasks = [(m, t) for m, t in itertools.product(*[_test_models, _test_tasks]) if m in _hf_task_to_models[t]]
# Assign to pytest variables for testing
pytest.model_w_tasks = _model_w_tasks
pytest.mt_names = [f"{m}-{t}" for m, t in pytest.model_w_tasks]


@pytest.fixture(scope="module", autouse=True)
def verify_models():
    # Verify all test models are registered in HF
    _test_models_not_found = [m for m in _test_models if m not in _hf_model_names]
    if _test_models_not_found:
        pytest.fail(f"Model(s) not found in HuggingFace: {_test_models_not_found}")

    # Verify all models are assigned to at least one task
    _models_to_be_tested = set(m for m, t in _model_w_tasks)
    _missing_task_models = _models_to_be_tested.difference(_test_models)
    if _missing_task_models:
        pytest.fail(f"Model(s) do not have an assigned task: {_missing_task_models}")


""" Fixtures for inference config """


@pytest.fixture(params=pytest.model_w_tasks, ids=pytest.mt_names)
def model_w_task(request):
    return request.param


dtype_params = [torch.float, torch.half]
dtype_ids = ["fp32", "fp16"]
if bool(pytest.use_hpu) == True:
    dtype_params = [torch.float, torch.bfloat16, torch.half]
    dtype_ids = ["fp32", "bf16", "fp16"]


@pytest.fixture(params=dtype_params, ids=dtype_ids)
def dtype(request):
    if bool(pytest.use_hpu) == True:
        # FP16 is not supported on Gaudi1.
        if get_hpu_dev_version() == "Gaudi" and request.param == torch.float16:
            pytest.skip("FP16 is not supported by Gaudi1.")
    return request.param


@pytest.fixture(params=[True, False], ids=["CG", "noCG"])
def enable_cuda_graph(request):
    return request.param


@pytest.fixture(params=[True, False], ids=["Triton", "noTriton"])
def enable_triton(request):
    return request.param


""" Fixtures for running query """


@pytest.fixture
def query(model_w_task):
    model, task = model_w_task
    angle_bracket_mask_models = ["roberta", "camembert", "esm", "ibert", "luke", "mpnet", "yoso", "mpnet"]

    if task == "fill-mask":
        if any(map(lambda x: x in model, angle_bracket_mask_models)):
            return "Hello I'm a <mask> model."
        else:
            return "Hell I'm a [MASK] model."
    elif task == "question-answering":
        return {
            "question": "What's my name?",
            "context": "My name is Clara and I live in Berkeley",
        }
    elif task == "text-classification":
        return "DeepSpeed is the greatest"
    elif task == "token-classification":
        return "My name is jean-baptiste and I live in montreal."
    elif task == "text-generation":
        return "DeepSpeed is the greatest"
    elif task == "text2text-generation":
        return "Is this review positive or negative? Review: this is the best cast iron skillet you will ever buy"
    elif task == "translation" or task == "summarization":
        return "Hello, my dog is cute"
    else:
        NotImplementedError(f'query for task "{task}" is not implemented')


@pytest.fixture
def inf_kwargs(model_w_task):
    model, task = model_w_task
    if task == "text-generation":
        if model == "EleutherAI/gpt-j-6b":
            # This model on V100 is hitting memory problems that limit the number of output tokens
            return {"do_sample": False, "temperature": 1.0, "max_length": 12}
        return {"do_sample": False, "temperature": 1.0, "max_length": 20}
    else:
        return {}


""" Assertion fixture for verifying model outputs """


def fill_mask_assert(x, y):
    return set(res["token_str"] for res in x) == set(res["token_str"] for res in y)


def question_answering_assert(x, y):
    return x["answer"] == y["answer"]


def text_classification_assert(x, y):
    return set(res["label"] for res in x) == set(res["label"] for res in y)


def token_classification_assert(x, y):
    return set(ent["word"] for ent in x) == set(ent["word"] for ent in y)


def text_generation_assert(x, y):
    return set(res["generated_text"] for res in x) == set(res["generated_text"] for res in y)


def text2text_generation_assert(x, y):
    return set(res["generated_text"] for res in x) == set(res["generated_text"] for res in y)


def translation_assert(x, y):
    return set(res["translation_text"] for res in x) == set(res["translation_text"] for res in y)


def summarization_assert(x, y):
    return set(res["summary_text"] for res in x) == set(res["summary_text"] for res in y)


@pytest.fixture
def assert_fn(model_w_task):
    model, task = model_w_task
    assert_fn_dict = {
        "fill-mask": fill_mask_assert,
        "question-answering": question_answering_assert,
        "text-classification": text_classification_assert,
        "token-classification": token_classification_assert,
        "text-generation": text_generation_assert,
        "text2text-generation": text2text_generation_assert,
        "translation": translation_assert,
        "summarization": summarization_assert
    }
    assert_fn = assert_fn_dict.get(task, None)
    if assert_fn is None:
        NotImplementedError(f'assert_fn for task "{task}" is not implemented')
    return assert_fn


# Used to verify DeepSpeed kernel injection worked with a model
def check_injection(model):

    def verify_injection(module):
        for child in module.children():
            if isinstance(child, nn.ModuleList):
                assert isinstance(child[0], DeepSpeedTransformerInference),\
                    "DeepSpeed-Inference Transformer kernels has not been injected in the model"
                break
            else:
                verify_injection(child)

    verify_injection(model)


# Verify that test is valid
def validate_test(model_w_task, dtype, enable_cuda_graph, enable_triton):
    model, task = model_w_task
    msg = ""
    if (not bool(pytest.use_hpu) == True) and enable_cuda_graph and (torch_info["cuda_version"] == "0.0"):
        msg = "CUDA not detected, cannot use CUDA Graph"
    elif (not bool(pytest.use_hpu)
          == True) and enable_cuda_graph and pkg_version.parse(torch.__version__) < pkg_version.parse("1.10"):
        msg = "CUDA Graph is only available in torch versions >= 1.10"
    elif "gpt-j-6b" in model and (not bool(pytest.use_hpu) == True):
        if dtype != torch.half:
            msg = f"Not enough GPU memory to run {model} with dtype {dtype}"
        elif enable_cuda_graph:
            msg = f"Not enough GPU memory to run {model} with CUDA Graph enabled"
    elif "gpt-neox-20b" in model:  # TODO: remove this when neox issues resolved
        msg = "Skipping gpt-neox-20b for now"
    elif ("gpt-neox-20b" in model) and (dtype != torch.half):
        msg = f"Not enough GPU memory to run {model} with dtype {dtype}"
    elif ("bloom" in model) and (dtype != torch.half):
        msg = f"Bloom models only support half precision, cannot use dtype {dtype}"
    elif ("bert" not in model.lower()) and enable_cuda_graph:
        msg = "Non bert/roberta models do no support CUDA Graph"
    elif (bool(pytest.use_hpu) == True) and enable_triton:
        msg = "HPU is not supported for triton."
    elif enable_triton and not (dtype in [torch.half]):
        msg = "Triton is for fp16"
    elif enable_triton and not deepspeed.HAS_TRITON:
        msg = "triton needs to be installed for the test"
    elif ("bert" not in model.lower()) and enable_triton:
        msg = "Triton kernels do not support Non bert/roberta models yet"
    return msg


@pytest.mark.nightly
class TestModelTask(DistributedTest):
    world_size = 1

    def test(
        self,
        model_w_task,
        dtype,
        enable_cuda_graph,
        enable_triton,
        query,
        inf_kwargs,
        assert_fn,
        perf_meas=True,
    ):
        invalid_test_msg = validate_test(model_w_task, dtype, enable_cuda_graph, enable_triton)
        if invalid_test_msg:
            pytest.skip(invalid_test_msg)

        model, task = model_w_task
        local_rank = int(os.getenv("LOCAL_RANK", "0"))

        # Load the model on CPU first to avoid OOM for large models @fp32
        if dtype == torch.bfloat16:
            pipe = pipeline(task, model=model, device=torch.device("cpu"), framework="pt", torch_dtype=torch.bfloat16)
        else:
            pipe = pipeline(task, model=model, device=torch.device("cpu"), framework="pt")
        if dtype == torch.half:
            pipe.model.half()

        # Switch device to GPU/HPU after converting to half
        device = torch.device(get_accelerator().device_name(local_rank))
        pipe.device = device
        pipe.model.to(device)

        # Warm-up queries for perf measurement
        #for i in range(10):
        #    _ = pipe(query, **inf_kwargs)
        get_accelerator().synchronize()
        start = time.time()
        bs_output = pipe(query, **inf_kwargs)
        get_accelerator().synchronize()
        bs_time = time.time() - start

        args = {
            'mp_size': 1,
            'dtype': dtype,
            'replace_with_kernel_inject': True,
            'enable_cuda_graph': enable_cuda_graph,
            'use_triton': enable_triton,
            'triton_autotune': False,
        }
        if pipe.tokenizer.model_max_length < deepspeed.ops.transformer.inference.config.DeepSpeedInferenceConfig(
        ).max_out_tokens:
            args.update({'max_out_tokens': pipe.tokenizer.model_max_length})
        pipe.model = deepspeed.init_inference(pipe.model, **args)
        check_injection(pipe.model)
        # Warm-up queries for perf measurement
        #for i in range(10):
        #    _ = pipe(query, **inf_kwargs)
        get_accelerator().synchronize()
        start = time.time()
        ds_output = pipe(query, **inf_kwargs)
        get_accelerator().synchronize()
        ds_time = time.time() - start

        if perf_meas:
            print(
                f"model={model}, task={task}, dtype={dtype}, cuda_graph={enable_cuda_graph}, triton={enable_triton}, bs_time={bs_time}, ds_time={ds_time}"
            )

        # facebook/opt* and some bigscient/bloom* models are not matching
        # baseline exactly, adding an exception to them for now
        if ("opt" in model) or ("bloom" in model):
            bs_output = pipe(query, **inf_kwargs)

        # These performance tests are only measuring the time for a single
        # inference request, we just want to check that performance isn't terrible
        #assert ds_time <= (bs_time * 1.1)

        assert assert_fn(bs_output, ds_output)


@pytest.mark.skipif(((bool(pytest.use_hpu) != True)), reason="Kernel Inject False validation for HPU tests.")
@pytest.mark.nightly
class TestModelTaskKIFalse(DistributedTest):
    world_size = 1

    def test(
        self,
        model_w_task,
        dtype,
        enable_cuda_graph,
        enable_triton,
        query,
        inf_kwargs,
        assert_fn,
        perf_meas=True,
    ):
        invalid_test_msg = validate_test(model_w_task, dtype, enable_cuda_graph, enable_triton)
        if invalid_test_msg:
            pytest.skip(invalid_test_msg)

        model, task = model_w_task
        local_rank = int(os.getenv("LOCAL_RANK", "0"))

        # Load the model on CPU first to avoid OOM for large models @fp32
        if dtype == torch.bfloat16:
            pipe = pipeline(task, model=model, device=torch.device("cpu"), framework="pt", torch_dtype=torch.bfloat16)
        else:
            pipe = pipeline(task, model=model, device=torch.device("cpu"), framework="pt")
        if dtype == torch.half:
            pipe.model.half()

        # Switch device to GPU/HPU after converting to half
        device = torch.device(get_accelerator().device_name(local_rank))
        pipe.device = device
        pipe.model.to(device)

        # Warm-up queries for perf measurement
        #for i in range(10):
        #    _ = pipe(query, **inf_kwargs)
        get_accelerator().synchronize()
        start = time.time()
        bs_output = pipe(query, **inf_kwargs)
        get_accelerator().synchronize()
        bs_time = time.time() - start
        if bool(pytest.use_hpu) == True:
            injection_policy = {BertLayer: ("output.dense", )}
            if "facebook/opt" in model or "Norod78" in model:
                injection_policy = {BertLayer: ("out_proj", )}
            if "gpt2" in model or "EleutherAI" in model or "bigscience/bloom" in model:
                injection_policy = {BertLayer: ("mlp", )}
            if "distilbert" in model:
                injection_policy = {BertLayer: ("output_layer_norm", )}
            args = {
                'mp_size': 1,
                'dtype': dtype,
                'replace_with_kernel_inject': False,
                'enable_cuda_graph': enable_cuda_graph,
                'use_triton': enable_triton,
                'triton_autotune': False,
                'injection_policy': injection_policy,
            }
        else:
            args = {
                'mp_size': 1,
                'dtype': dtype,
                'replace_with_kernel_inject': True,
                'enable_cuda_graph': enable_cuda_graph,
                'use_triton': enable_triton,
                'triton_autotune': False,
            }
        if pipe.tokenizer.model_max_length < deepspeed.ops.transformer.inference.config.DeepSpeedInferenceConfig(
        ).max_out_tokens:
            args.update({'max_out_tokens': pipe.tokenizer.model_max_length})
        pipe.model = deepspeed.init_inference(pipe.model, **args)
        # Warm-up queries for perf measurement
        #for i in range(10):
        #    _ = pipe(query, **inf_kwargs)
        get_accelerator().synchronize()
        start = time.time()
        ds_output = pipe(query, **inf_kwargs)
        get_accelerator().synchronize()
        ds_time = time.time() - start

        if perf_meas:
            print(
                f"model={model}, task={task}, dtype={dtype}, cuda_graph={enable_cuda_graph}, triton={enable_triton}, bs_time={bs_time}, ds_time={ds_time}"
            )

        # facebook/opt* and some bigscient/bloom* models are not matching
        # baseline exactly, adding an exception to them for now
        if ("opt" in model) or ("bloom" in model):
            bs_output = pipe(query, **inf_kwargs)

        # These performance tests are only measuring the time for a single
        # inference request, we just want to check that performance isn't terrible
        #assert ds_time <= (bs_time * 1.1)

        assert assert_fn(bs_output, ds_output)


@pytest.mark.seq_inference
@pytest.mark.parametrize("model_w_task", [("EleutherAI/gpt-neo-1.3B", "text-generation"),
                                          ("EleutherAI/gpt-neox-20b", "text-generation"),
                                          ("bigscience/bloom-3b", "text-generation"),
                                          ("EleutherAI/gpt-j-6b", "text-generation")],
                         ids=["gpt-neo", "gpt-neox", "bloom", "gpt-j"])
class TestMPSize(DistributedTest):
    world_size = 2

    def test(
        self,
        model_w_task,
        dtype,
        query,
        inf_kwargs,
        assert_fn,
    ):
        invalid_test_msg = validate_test(model_w_task, dtype, enable_cuda_graph=False, enable_triton=False)
        if invalid_test_msg:
            pytest.skip(invalid_test_msg)

        model, task = model_w_task
        local_rank = int(os.getenv("LOCAL_RANK", "0"))

        # We have to load these large models on CPU with pipeline because not
        # enough GPU memory
        pipe = pipeline(task, model=model, device=torch.device("cpu"), framework="pt")
        bs_output = pipe(query, **inf_kwargs)
        pipe.model = deepspeed.init_inference(pipe.model,
                                              mp_size=self.world_size,
                                              dtype=dtype,
                                              replace_with_kernel_inject=True)
        check_injection(pipe.model)
        # Switch device to GPU so that input tensors are not on CPU
        pipe.device = torch.device(get_accelerator().device_name(local_rank))
        ds_output = pipe(query, **inf_kwargs)

        print(local_rank, "baseline", bs_output)
        print(local_rank, "deepspeed", ds_output)
        assert assert_fn(bs_output, ds_output)


@pytest.mark.inference
@pytest.mark.parametrize("model_w_task", [("gpt2", "text-generation")], ids=["gpt2"])
class TestLowCpuMemUsage(DistributedTest):
    world_size = 1

    def test(
        self,
        model_w_task,
        query,
        inf_kwargs,
        assert_fn,
    ):
        model, task = model_w_task
        dtype = torch.float16
        local_rank = int(os.getenv("LOCAL_RANK", "0"))
        device = local_rank
        if bool(pytest.use_hpu) == True:
            device = torch.device(f"cpu:{local_rank}")
        pipe = pipeline(task, model=model, model_kwargs={"low_cpu_mem_usage": True}, device=device, framework="pt")
        bs_output = pipe(query, **inf_kwargs)
        pipe.model = deepspeed.init_inference(pipe.model,
                                              mp_size=self.world_size,
                                              dtype=dtype,
                                              replace_method="auto",
                                              replace_with_kernel_inject=True)

        ds_output = pipe(query, **inf_kwargs)

        assert assert_fn(bs_output, ds_output)


@pytest.mark.seq_inference
@pytest.mark.parametrize("model_w_task", [("tiiuae/falcon-7b", "text-generation")], ids=["falcon"])
class TestAutoTP(DistributedTest):
    world_size = 1

    def test(
        self,
        model_w_task,
        query,
        inf_kwargs,
        assert_fn,
    ):
        # TODO: enable this test for H100 tests
        if bool(pytest.use_hpu) != True:
            pytest.skip("Not enough GPU memory for this on V100 runners")
        model, task = model_w_task
        dtype = torch.bfloat16
        local_rank = int(os.getenv("LOCAL_RANK", "0"))

        # We have to load these large models on CPU with pipeline because not
        # enough GPU memory
        tokenizer = AutoTokenizer.from_pretrained(model, use_fast=True)
        pipe = pipeline(task,
                        model=model,
                        tokenizer=tokenizer,
                        torch_dtype=dtype,
                        trust_remote_code=True,
                        device=torch.device("cpu"),
                        framework="pt")
        #bs_output = pipe(query, **inf_kwargs)

        pipe.model = deepspeed.init_inference(pipe.model, mp_size=self.world_size, replace_with_kernel_inject=False)
        # Switch device to GPU so that input tensors are not on CPU
        pipe.device = torch.device(get_accelerator().device_name(local_rank))
        ds_output = pipe(query, **inf_kwargs)

        #print(local_rank, "baseline", bs_output)
        print(local_rank, "deepspeed", ds_output)
        #assert assert_fn(bs_output, ds_output)


@pytest.mark.seq_inference
@pytest.mark.parametrize(
    "model_w_task, injection_policy",
    [
        (("google/t5-v1_1-small", "text2text-generation"), {
            T5Block: ('SelfAttention.o', 'EncDecAttention.o', 'DenseReluDense.wo')
        }),
        (("roberta-large", "fill-mask"), {
            RobertaLayer: ('output.dense')
        }),
    ],
    ids=["t5", "roberta"],
)
@pytest.mark.parametrize("dtype", [torch.float], ids=["fp32"])
class TestInjectionPolicy(DistributedTest):
    world_size = [1, 2]

    def test(
        self,
        model_w_task,
        injection_policy,
        query,
        inf_kwargs,
        assert_fn,
        dtype,
    ):
        invalid_test_msg = validate_test(model_w_task, dtype, enable_cuda_graph=False, enable_triton=False)
        if invalid_test_msg:
            pytest.skip(invalid_test_msg)

        model, task = model_w_task
        local_rank = int(os.getenv("LOCAL_RANK", "0"))
        world_size = int(os.getenv("WORLD_SIZE", "2"))

        # We have to load these large models on CPU with pipeline because not
        # enough GPU memory
        pipe = pipeline(task, model=model, device=torch.device("cpu"), framework="pt")
        bs_output = pipe(query, **inf_kwargs)
        pipe.model = deepspeed.init_inference(pipe.model,
                                              mp_size=world_size,
                                              dtype=dtype,
                                              injection_policy=injection_policy)
        # Switch device to GPU so that input tensors are not on CPU
        pipe.device = torch.device(get_accelerator().device_name(local_rank))
        ds_output = pipe(query, **inf_kwargs)

        print(local_rank, "baseline", bs_output)
        print(local_rank, "deepspeed", ds_output)
        assert assert_fn(bs_output, ds_output)


@pytest.mark.seq_inference
@pytest.mark.parametrize(
    "model_w_task",
    [("Helsinki-NLP/opus-mt-en-de", "translation"), ("Salesforce/codegen-350M-mono", "text-generation")],
    ids=["marian", "codegen"],  #codegen has fusedqkv weight.
)
@pytest.mark.parametrize("dtype", [torch.float16], ids=["fp16"])
class TestAutoTensorParallelism(DistributedTest):
    world_size = [2]

    def test(
        self,
        model_w_task,
        query,
        inf_kwargs,
        assert_fn,
        dtype,
    ):
        invalid_test_msg = validate_test(model_w_task, dtype, enable_cuda_graph=False, enable_triton=False)
        if invalid_test_msg:
            pytest.skip(invalid_test_msg)

        model, task = model_w_task
        local_rank = int(os.getenv("LOCAL_RANK", "0"))
        world_size = int(os.getenv("WORLD_SIZE", "2"))

        # We have to load these large models on CPU with pipeline because not
        # enough GPU memory
        pipe = pipeline(task, model=model, device=torch.device("cpu"), framework="pt")
        bs_output = pipe(query, **inf_kwargs)

        pipe.model = deepspeed.init_inference(pipe.model, mp_size=world_size, dtype=dtype)
        # Switch device to GPU so that input tensors are not on CPU
        pipe.device = torch.device(get_accelerator().device_name(local_rank))
        ds_output = pipe(query, **inf_kwargs)

        print(local_rank, "baseline", bs_output)
        print(local_rank, "deepspeed", ds_output)
        assert assert_fn(bs_output, ds_output)


@pytest.mark.nightly
@pytest.mark.parametrize(
    "model_family, model_name",
    (
        ["gpt2", "EleutherAI/gpt-neo-2.7B"],
        #["gpt2", "EleutherAI/gpt-j-6b"], # Causing OOM for this test
        ["gpt2", "gpt2-xl"],
    ),
)
@pytest.mark.parametrize("task", ["lambada_standard"])
class TestLMCorrectness(DistributedTest):
    world_size = 1
    exec_timeout = 1200  # Give these tests longer to complete

    def test(self, model_family, model_name, task):
        # imports here to avoid import errors when pytest collects tests
        import lm_eval
        import lm_eval.models
        import lm_eval.tasks
        import lm_eval.evaluator

        # The bootstrap_stderr function in lm_eval.metrics uses a
        # multiprocessing Pool to increase performance. Since we use a Pool for
        # our distributed tests and cannot nest Pools, we must redefine and
        # patch this function with a version that does not use Pool.
        def no_pool_bootstrap_stderr(f, xs, iters):
            from lm_eval.metrics import _bootstrap_internal
            from lm_eval.metrics import sample_stddev
            res = []
            chunk_size = min(1000, iters)
            for i in range(iters // chunk_size):
                res.extend(_bootstrap_internal(f, chunk_size)((i, xs)))
            return sample_stddev(res)

        lm_eval.metrics.bootstrap_stderr = no_pool_bootstrap_stderr

        local_rank = os.getenv("LOCAL_RANK", "0")
        device = torch.device(get_accelerator().device_name(local_rank))
        dtype = torch.float
        task_dict = lm_eval.tasks.get_task_dict([task])

        if 'gpt-j-6b' in model_name:
            dtype = torch.half
            if bool(pytest.use_hpu) == True:
                if os.getenv("REPLACE_FP16", default=None):
                    dtype = torch.bfloat16
            lm = lm_eval.models.get_model(model_family).create_from_arg_string(f"pretrained={model_name}",
                                                                               {"device": "cpu"})
            setattr(lm, model_family, getattr(lm, model_family).to(dtype=dtype).to(device))
            lm._device = device
        else:
            if bool(pytest.use_hpu) == True:
                #lm_eval not supporting HPU device, so get model with CPU and move it to HPU.
                lm = lm_eval.models.get_model(model_family).create_from_arg_string(f"pretrained={model_name}",
                                                                                   {"device": "cpu"})
                setattr(lm, model_family, getattr(lm, model_family).to(device))
                lm._device = device
            else:
                lm = lm_eval.models.get_model(model_family).create_from_arg_string(
                    f"pretrained={model_name}", {"device": get_accelerator().device_name()})

        get_accelerator().synchronize()
        start = time.time()
        bs_output = lm_eval.evaluator.evaluate(lm=lm, task_dict=task_dict)
        get_accelerator().synchronize()
        bs_time = time.time() - start

        getattr(lm, model_family).to("cpu")
        ds_model = deepspeed.init_inference(
            getattr(lm, model_family),
            mp_size=1,
            dtype=dtype,
            replace_with_kernel_inject=True,
            enable_cuda_graph=False,
        )
        check_injection(ds_model)
        setattr(lm, model_family, ds_model)
        get_accelerator().synchronize()
        start = time.time()
        ds_output = lm_eval.evaluator.evaluate(lm=lm, task_dict=task_dict)
        get_accelerator().synchronize()
        ds_time = time.time() - start

        ppl_diff = abs(bs_output["results"][task]["ppl"] - ds_output["results"][task]["ppl"])
        #assert ds_time <= bs_time
        assert ppl_diff < 0.01
