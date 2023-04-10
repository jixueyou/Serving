import grpc
import sys
from concurrent import futures
import logging.config
import logging

import threading
import os
from threading import Thread
import time
from typing import TypeVar, Union, Generic, List
import tarfile
import shutil

# sys.path.append("proto")
from .proto import model_hot_load_service_pb2_grpc
from .proto import model_hot_load_service_pb2
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


class AsyncFuture(object):

    def __init__(self, callback):
        self._callback = callback
        self._lock = threading.Lock()
        self._notNull = threading.Condition(self._lock)
        self._completed = False
        self._result = None

    def wait_completed(self, timeout):
        self._lock.acquire()
        try:
            if not self._completed:
                self._notNull.wait(timeout)
        finally:
            self._lock.release()
        return self._result

    def completed(self, result):
        self._lock.acquire()
        try:
            if not self._completed:
                self._result = result
                self._completed = True
                self._notNull.notify_all()
        finally:
            self._lock.release()

    def do_callback_func(self):
        return self._callback()

    def is_done(self):
        return self._completed


T = TypeVar("T", bound=Union[AsyncFuture])


class ReloadQueue(Generic[T]):
    def __init__(self, size):
        self._size = size
        self._lock = threading.RLock()
        self._notFull = threading.Condition(self._lock)
        self._notEmpty = threading.Condition(self._lock)
        self._data: List[T] = []

    def put(self, item: T):
        self._lock.acquire()
        try:
            while len(self._data) == self._size:
                self._notFull.wait()
            self._data.append(item)
            self._notEmpty.notify()
        finally:
            self._lock.release()

    def take(self):
        self._lock.acquire()
        try:
            while len(self._data) == 0:
                self._notEmpty.wait()
            item = self._data.pop(0)
            self._notFull.notify()
        finally:
            self._lock.release()
        return item

    def size(self):
        self._lock.acquire()
        try:
            size = len(self._data)
        finally:
            self._lock.release()
        return size

    def saturated(self):
        self._lock.acquire()
        try:
            return self.size() >= self.capacity()
        finally:
            self._lock.release()

    def capacity(self):
        return self._size


class Reloader(ReloadQueue):

    def __init__(self, queue_size, ):
        super(Reloader, self).__init__(queue_size)

    def loop(self):
        while True:
            future = self.take()
            try:
                future.do_callback_func()
                future.completed(True)
            except Exception as ex:
                future.completed(False)
                _LOGGER.error('Reloader hot load error: {}.'.format(repr(ex)))


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
        return self._exec_hot_load_model(request.model_name, request.model_file_address, request.is_remote,
                                         request.is_tar_packed,
                                         request.timeout)

    def _check_params(self, params):
        for param in params:
            if getattr(self, param, None) is None:
                raise Exception('{} not set.'.format(param))

    def _print_params(self, params_name):
        self._check_params(params_name)
        for name in params_name:
            _LOGGER.info('{}: {}'.format(name, getattr(self, name)))

    def _get_local_timestamp_file_path(self, model_name):
        return os.path.join(self._local_model_path, model_name, self._local_model_timestamp_file_name)

    def _get_local_model_path(self, model_name):
        return os.path.join(self._local_model_path, model_name)

    def _get_local_model_reload_time_stamp(self, model_name):
        local_model_path = self._get_local_model_path(model_name)
        reload_time_file = os.path.join(local_model_path, 'reload_time_file')
        if os.path.exists(reload_time_file):
            return os.stat(reload_time_file).st_mtime
        else:
            return 0

    def _pull_remote_model(self, model_name, model_file_address, is_tar_packed):
        _LOGGER.info('Pull model file address: {}'.format(model_file_address))
        tmp_model_path = os.path.join(self._local_tmp_path, model_name)
        # 删除已存在的模型目录
        if os.path.exists(tmp_model_path):
            if os.path.isfile(tmp_model_path):
                os.remove(tmp_model_path)
            else:
                shutil.rmtree(tmp_model_path)
        (_, file_name) = os.path.split(model_file_address)
        if not is_tar_packed:
            # 拉取远程模型目录.
            cmd = 'wget -nH -r -P {} {} > /dev/null 2>&1'.format(self._local_tmp_path, model_file_address)
        else:
            # 拉取远程模型打包文件
            cmd = 'wget -nd -N -P {} {} > /dev/null 2>&1'.format(self._local_tmp_path, model_file_address)
        _LOGGER.info('Pull model file, wget cmd: {}'.format(cmd))
        if os.system(cmd) != 0:
            raise Exception('pull remote model file {}, failed.'.format(model_file_address))
        else:
            pull_model_path = os.path.join(self._local_tmp_path, file_name)
            if is_tar_packed:
                _LOGGER.info("Try to unpack remote file({})".format(pull_model_path))
                if not tarfile.is_tarfile(pull_model_path):
                    raise Exception('Not a tar packaged file type. {}'.format(pull_model_path))
                try:
                    # 解压模型文件
                    tar = tarfile.open(pull_model_path)
                    tar.extractall(self._local_tmp_path)
                    tar.close()
                    unpacked_model_path = os.path.join(self._local_tmp_path, file_name.split('.')[0])
                    # 更改模型目录名称
                    os.rename(unpacked_model_path, tmp_model_path)
                except Exception as ex:
                    raise Exception('Decompressing failed, error {}'.format(repr(ex)))
                finally:
                    os.remove(pull_model_path)
            else:
                os.rename(tmp_model_path, tmp_model_path)
        return tmp_model_path

    def _update_local_donefile(self, model_name):
        done_file_path = self._get_local_timestamp_file_path(model_name)
        cmd = 'touch {}'.format(done_file_path)
        _LOGGER.info('update done timestamp cmd: {}'.format(cmd))
        if os.system(cmd) != 0:
            raise Exception('update local done file failed.')

    def _hot_load(self, model_name, model_file_address, is_remote, is_tar_packed, timeout):
        start_timestamp = time.time()
        prime_reload_timestamp = self._get_local_model_reload_time_stamp(model_name)
        _LOGGER.info('Hot load model_name: {}, start timestamp: {}, '
                     'prime reload timestamp: {}.'.format(model_name, start_timestamp, prime_reload_timestamp))
        # 拉取模型文件
        if is_remote:
            new_local_model_path = self._pull_remote_model(model_name, model_file_address, is_tar_packed)
        else:
            new_local_model_path = model_file_address
        # 更新模型文件
        self._update_local_model(model_name, new_local_model_path)

        # 等待模型更新完成
        while True:
            current_reload_timestamp = self._get_local_model_reload_time_stamp(model_name)
            current_timestamp = time.time()
            if current_reload_timestamp > prime_reload_timestamp:
                _LOGGER.info('Hot load model_name: {} succeeded.'.format(model_name))
                break
            if current_timestamp - start_timestamp > timeout:
                _LOGGER.info('Hot load model_name: {} timeout.'.format(model_name))
            time.sleep(1)
            _LOGGER.info('Hot load model_name: {} waiting for update to succeed.'.format(model_name))

    def _update_local_model(self, model_name, new_local_model_path):
        cmd = 'cp -r {}/* {}'.format(new_local_model_path, self._get_local_model_path(model_name))
        _LOGGER.info('Update model cmd: {}'.format(cmd))
        if os.system(cmd) != 0:
            raise Exception('Update local model failed.')

    def _exec_hot_load_model(self, model_name, model_file_address, is_remote, is_tar_packed, timeout):
        _LOGGER.info(
            f"Exec hot load model_name: {model_name}, model_file_url: {model_file_address}, is_remote: {is_remote}, "
            f"is_tar_packed: {is_tar_packed}, timeout: {timeout}.")
        if model_name in self._local_model_reloader_map:
            reloader = self._local_model_reloader_map[model_name]
            saturated = reloader.saturated()
            if saturated:
                return model_hot_load_service_pb2.Response(err_no=10001,
                                                           err_msg='Exec hot load model_name {} queue saturated'.format(
                                                               model_name))
            do_hot_load = lambda: self._hot_load(model_name, model_file_address, is_remote, is_tar_packed, timeout)
            future = AsyncFuture(callback=do_hot_load)
            # 放入处理队列
            reloader.put(future)
            # 等待热加载完成
            ok = future.wait_completed(timeout)
            if ok:
                return model_hot_load_service_pb2.Response(err_no=0, err_msg='ok')
            else:
                return model_hot_load_service_pb2.Response(err_no=10002,
                                                           err_msg='Exec hot load model_name {} failed'.format(
                                                               model_name))
        else:
            return model_hot_load_service_pb2.Response(err_no=9999,
                                                       err_msg='Exec hot load model_name {} not found.'.format(
                                                           model_name))

    def serve(self):
        if not os.path.exists(self._local_tmp_path):
            _LOGGER.info('mkdir: {}'.format(self._local_tmp_path))
            os.makedirs(self._local_tmp_path)

        for model_name in self._local_model_names:
            model_path = self._get_local_model_path(model_name)
            if os.path.isdir(model_path):
                pass
            elif os.path.isfile(model_path):
                raise ValueError('model path: {}, should be a dir not file.'.format(model_path))
            reloader = Reloader(self._reload_queue_size)
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
