import grpc
import sys
from concurrent import futures
import logging.config
import logging
import socket
import threading
from queue import Queue
import os
from threading import Thread
from http.server import SimpleHTTPRequestHandler
from http.server import CGIHTTPRequestHandler
from http.server import ThreadingHTTPServer
import contextlib
from functools import partial
import time

sys.path.append("proto")
from proto import model_hot_load_service_pb2_grpc
from proto import model_hot_load_service_pb2
import argparse

log_dir = "logs"
if 'HOT_LOAD_LOG_PATH' in os.environ:
    hot_load_log_path = os.environ['HOT_LOAD_LOG_PATH']
    log_dir = os.path.join(hot_load_log_path, log_dir)
else:
    log_dir = os.path.join(os.getcwd(), log_dir)
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logger_config = {
    "version": 1,
    "formatters": {
        "console_fmt": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(lineno)d@%(filename)s - %(message)s",
        },
        "file_fmt": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(process)d - %(lineno)d@%(filename)s - %(message)s",
        },
    },
    "handlers": {
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "DEBUG",
            "formatter": "file_fmt",
            "filename": os.path.join(log_dir, "hot_load.log"),
            "maxBytes": 512000000,
            "backupCount": 20,
        },
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "console_fmt",
            "stream": "ext://sys.stdout",
        }
    },
    "root": {
        "level": "DEBUG",
        "handlers": ["file", "console"],
    },
}
logging.config.dictConfig(logger_config)

_LOGGER = logging.getLogger(__name__)


class ReloadQueue(object):
    def __init__(self, size):
        self._size = size
        self._lock = threading.Lock()
        self._notFull = threading.Condition(self._lock)
        self._notEmpty = threading.Condition(self._lock)
        self._data = []

    def put(self, item):
        self._lock.acquire()
        while len(self._data) == self._size:
            self._notFull.wait()
        self._data.append(item)
        self._notEmpty.notify()
        self._lock.release()

    def take(self):
        self._lock.acquire()
        while len(self._data) == 0:
            self._notEmpty.wait()
        item = self._data.pop(0)
        self._notFull.notify()
        self._lock.release()
        return item

    def size(self):
        self._lock.acquire()
        size = len(self._data)
        self._lock.release()
        return size


class Reloader(ReloadQueue):

    def __init__(self, queue_size, local_model_path, local_timestamp_path):
        super(Reloader, self).__init__(queue_size)
        self._local_model_path = local_model_path
        self._local_timestamp_path = local_timestamp_path

    def loop(self):
        while True:
            print('正在执行reload......{} \n'.format(self._local_model_path), end='')
            time.sleep(1)


class ModelHotLoadService(model_hot_load_service_pb2_grpc.HotLoadModelService):

    def __init__(self, grpc_port,
                 grpc_max_workers,
                 reload_queue_size,
                 local_tmp_path,
                 local_model_path,
                 local_model_names,
                 local_model_timestamp_file_name):
        params = [
            '_grpc_port', '_grpc_max_workers', '_local_model_path', '_local_tmp_path',
            '_reload_queue_size', '_local_model_names', '_local_model_timestamp_file_name'
        ]
        self._grpc_port = grpc_port
        self._grpc_max_workers = grpc_max_workers
        self._reload_queue_size = reload_queue_size
        self._local_tmp_path = local_tmp_path
        self._local_model_path = local_model_path
        self._local_model_names = local_model_names
        self._local_model_timestamp_file_name = local_model_timestamp_file_name
        self._local_model_reloader_map = {}
        self._print_params(params)

    def loading(self, request, context):
        _LOGGER.info(
            f"model_name: {request.model_name}  model_file_url: {request.model_file_address} remote: {request.remote}")
        # todo 完成下载模型文件并且放到制定目录
        return model_hot_load_service_pb2.Response(err_no=0, err_msg='ok')

    def _check_params(self, params):
        for param in params:
            if getattr(self, param, None) is None:
                raise Exception('{} not set.'.format(param))

    def _print_params(self, params_name):
        self._check_params(params_name)
        for name in params_name:
            _LOGGER.info('{}: {}'.format(name, getattr(self, name)))

    def _get_local_timestamp_file_path(self):
        return os.path.join(self._local_model_path, self._local_model_timestamp_file_name)

    def _get_local_model_path(self, model_name):
        return os.path.join(self._local_model_path, model_name)

    def serve(self):
        for model_name in self._local_model_names:
            model_path = self._get_local_model_path(model_name)
            if os.path.isdir(model_path):
                pass
            elif os.path.isfile(model_path):
                raise ValueError('model path: {}, should be a dir not file.'.format(model_path))
            reloader = Reloader(self._reload_queue_size, model_path, self._get_local_timestamp_file_path)
            self._local_model_reloader_map[model_name] = reloader
            Thread(target=reloader.loop, args=[], daemon=True).start()

        grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=self._grpc_max_workers))
        model_hot_load_service_pb2_grpc.add_HotLoadModelServiceServicer_to_server(self, grpc_server)
        grpc_server.add_insecure_port('[::]:' + str(self._grpc_port))
        _LOGGER.info(f"Grpc service started on port {self._grpc_port}.")
        grpc_server.start()
        # 开启reloader
        _LOGGER.info('Model hot load service started.')

        try:
            grpc_server.wait_for_termination()
        except KeyboardInterrupt:
            grpc_server.stop(1)
            _LOGGER.info('Model hot load service stopped.')
            sys.exit()


def serve_args():
    parser = argparse.ArgumentParser("HotLoadService")

    parser.add_argument(
        "--port",
        type=int,
        default=9099,
        help="Port of the starting grpc server")

    parser.add_argument(
        "--thread_num",
        type=int,
        default=10,
        nargs="+",
        help="Number of the starting grpc server worker")

    parser.add_argument(
        "--reload_queue_size",
        type=int,
        default=8,
        help="Number of the model reload queue size")

    parser.add_argument(
        "--local_tmp_path",
        type=str,
        default=os.path.join(os.getcwd(), '_serving_hot_load_tmp'),
        help="The path of the folder where temporary files are stored locally. If it does not exist, it will be created automatically"
    )

    parser.add_argument(
        "--local_model_path",
        type=str,
        required=True,
        help="Local model work path")
    #

    parser.add_argument(
        "--local_model_names",
        type=str,
        required=True,
        nargs="+",
        help="Local hot load model names")

    parser.add_argument(
        "--local_model_timestamp_file_name",
        type=str,
        default='fluid_time_file',
        help="The timestamp file used locally for hot loading, The file is considered to be placed in the `local_path/local_model_name` folder."
    )

    return parser.parse_args()


if __name__ == '__main__':
    args = serve_args()
    ModelHotLoadService(args.port,
                        args.thread_num,
                        args.reload_queue_size,
                        args.local_tmp_path,
                        args.local_model_path,
                        args.local_model_names,
                        args.local_model_timestamp_file_name).serve()
