import argparse
import asyncio
import os
import time
from typing import List, Set

import aiohttp
import uvicorn
import uvloop
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from utils import call_kv_method, serialize_seq_group

from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.sampling_params import SamplingParams
from vllm.usage.usage_lib import UsageContext
from vllm.utils import make_async

# global states for this perfill worker
engine = None
app = FastAPI()
client_session: aiohttp.ClientSession


def free_blocks(blocks):
    block_manager = engine.engine.scheduler.block_manager
    block_manager._free_block_table(blocks)


@app.post("/prefill")
async def prefill(request: Request) -> Response:
    """Generate completion for the request.

    The request should be a JSON object with the following fields:
    - prompt: the prompt to use for the generation.
    - request_id: the request id.
    - other fields: the sampling parameters (See `SamplingParams` for details).
    """
    request_dict = await request.json()
    prompt = request_dict.pop("prompt")
    request_id = request_dict.pop("request_id")
    kv_addr = request_dict.pop("kv_addr")
    sampling_params = SamplingParams(**request_dict)

    assert engine is not None
    await call_kv_method(engine, "add_kv_addr", request_id, kv_addr)
    results_generator = engine.generate(prompt, sampling_params, request_id)

    blocks = []
    final_output = None
    async for request_output in results_generator:
        if engine.is_prefill_worker and not args.enable_chunked_prefill:
            assert final_output is None, "Only one output is expected"
        final_output = request_output
        seq_group = final_output.seq_group
        blocks += final_output.blocks
    assert final_output is not None
    seq_group_data = await make_async(serialize_seq_group)(seq_group)
    await call_kv_method(engine, "finish_push_kv", request_id)
    free_blocks(blocks)
    return JSONResponse({"seq_group": seq_group_data})


async def run(config: uvicorn.Config):
    global client_session, engine, kv_info
    engine.start_background_loop()
    engine.set_prefill_worker()
    kv_info = await call_kv_method(
        engine, "prefill_kv_init", args.num_layer_per_push
    )
    client_session = aiohttp.ClientSession()
    server = uvicorn.Server(config=config)
    await server.serve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default=None)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", type=str, default="debug")
    parser.add_argument(
        "--model-path", type=str, default="/data/mistral-7b-instruct-v0_2"
    )
    parser.add_argument("--num-layer-per-push", type=int, default=8)
    parser = AsyncEngineArgs.add_cli_args(parser)
    args = parser.parse_args()
    args.model = args.model_path
    args.enforce_eager = True
    args.disable_custom_all_reduce = True
    args.engine_use_ray = False
    args.worker_use_ray = True
    engine_args = AsyncEngineArgs.from_cli_args(args)

    os.environ["RAY_NUM_CPUS"] = "64"
    os.environ["WORKER_MODULE"] = "worker"
    os.environ["WORKER_CLASS"] = "WorkerSplitwise"

    engine = AsyncLLMEngine.from_engine_args(
        engine_args, usage_context=UsageContext.API_SERVER
    )

    config = uvicorn.Config(
        app=app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    asyncio.run(run(config))