import asyncio

import torch
import torch.multiprocessing as mp
import ucp
import utils

import block_copy


class KVComm(mp.Process):
    def __init__(
        self,
        device: torch.device,
        dtype: torch.dtype,
        shape: tuple,
        role: str,
        requests_queue: mp.Queue,
        flags: dict, 
        server_port=None,
    ):
        super().__init__(name=f"KVComm:{device.index}")
        self.role = role
        self.device = device
        self.dtype = dtype
        self.requests_queue = requests_queue
        self.server_port = server_port
        self.flags = flags
        assert role == "client" or server_port is not None
        assert role == "server" or flags is not None
        assert len(shape) == 6
        assert shape[1] == 2
        self.cache_shape = shape
        self.block_shape = shape[3:]
        self.num_packing_blocks = shape[0] * shape[1] * 2

    async def _kv_server_handler(self, ep: ucp.Endpoint):
        id_tensor = utils.get_empty_uuid_tensor(self.device)
        await ep.recv(id_tensor)
        request_id = utils.tensor_to_uuid(id_tensor)
        while request_id not in self.pending_requests:
            await asyncio.sleep(0)
        tensor_data = self.pending_requests.pop(request_id)
        blocks = [func(*args) for (func, args) in tensor_data]
        buffer = self.get_buffer()
        for blks in utils.chunk(blocks, self.num_packing_blocks):
            await ep.recv(buffer)
            self.block_copy.scatter(blks, buffer)
        self.flags[request_id] = True
        await ep.close()

    def kv_server(self):
        """ The server will listen for KV blocks
        """
        async def _kv_server():
            self.pending_requests = {}
            self.lf = ucp.create_listener(
                self._kv_server_handler, self.server_port
            )
            while not self.lf.closed():
                if not self.requests_queue.empty():
                    request_id, tensor_data = self.requests_queue.get()
                    self.pending_requests[request_id] = tensor_data
                await asyncio.sleep(0)

        asyncio.run(_kv_server())

    async def _kv_push(self, host, port, request_id, tensor_queue):
        ep = await ucp.create_endpoint(host, port)
        id_tensor = utils.uuid_to_tensor(request_id, self.device)
        await ep.send(id_tensor)
        while tensor_data := await tensor_queue.get():
            buffer = self.get_buffer()
            blocks = [func(*args) for (func, args) in tensor_data]
            ep.flush()
            for blks in utils.chunk(blocks, self.num_packing_blocks):
                self.block_copy.gather(blks, buffer)
                await ep.send(buffer)
        await ep.close()

    def kv_client(self):
        """ The server will send KV blocks
        """
        async def _kv_client():
            tasks = {}
            while True:
                if not self.requests_queue.empty():
                    request_id, host, port, tensor_data = (
                        self.requests_queue.get(True)
                    )
                    if request_id not in tasks:
                        queue = asyncio.Queue()
                        task = asyncio.create_task(
                            self._kv_push(host, port, request_id, queue),
                            name = request_id,
                        )
                        tasks[request_id] = (task, queue)
                        task.add_done_callback(
                            lambda task: tasks.pop(task.get_name())
                        )
                    tasks[request_id][1].put_nowait(tensor_data)
                await asyncio.sleep(0)

        asyncio.run(_kv_client())

    def get_buffer(self) -> torch.Tensor:
        # TODO: We can use double buffer here
        buffer = torch.empty(
            (self.num_packing_blocks, *self.block_shape),
            device=self.device,
            dtype=self.dtype,
        )
        return utils.wrap_tensor(buffer)

    def run(self):
        torch.cuda.init()
        block_size = (
            self.cache_shape[3]
            * self.cache_shape[4]
            * self.cache_shape[5]
            * self.dtype.itemsize
            // 16
        )
        self.block_copy = block_copy.get_block_copy(
            self.num_packing_blocks, block_size
        )
        utils.set_NIC(self.device.index)
        method_map = {"server": self.kv_server, "client": self.kv_client}
        method_map[self.role]()
