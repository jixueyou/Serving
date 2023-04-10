from __future__ import print_function
import logging
import grpc

import sys
from threading import Thread

sys.path.append("proto")
from proto import model_hot_load_service_pb2_grpc
from proto import model_hot_load_service_pb2


def run_hot_load_uci_housing_model():
    with grpc.insecure_channel('10.0.197.217:9099') as channel:
        stub = model_hot_load_service_pb2_grpc.HotLoadModelServiceStub(channel)
        response = stub.loading(model_hot_load_service_pb2.Request(
            model_name='uci_housing_model',
            model_file_address='http://10.1.130.16:8080/uci_housing_model.tar.gz',
            # model_file_address='/Users/liujing/PycharmProjects/hot-load/tmp/uci_housing_model',
            # model_file_address='http://localhost:8080/uci_housing_model',
            is_remote=True,
            is_tar_packed=True,
            timeout=300
        ))
    print(f'run hot load uci_housing_model code: {response.err_no}, msg: {response.err_msg}')


def run_hot_load_ocr_model():
    with grpc.insecure_channel('10.0.197.217:9099') as channel:
        stub = model_hot_load_service_pb2_grpc.HotLoadModelServiceStub(channel)
        response = stub.loading(model_hot_load_service_pb2.Request(
            model_name='ocr_model',
            model_file_address='http://10.1.130.16:8080/ocr_model.tar.gz',
            # model_file_address='/Users/liujing/PycharmProjects/hot-load/tmp/uci_housing_model',
            # model_file_address='http://localhost:8080/uci_housing_model',
            is_remote=True,
            is_tar_packed=True,
            timeout=300
        ))
    print(f'run hot load ocr_model code: {response.err_no}, msg: {response.err_msg}')


# 测试一下
if __name__ == '__main__':
    logging.basicConfig()
    Thread(target=run_hot_load_uci_housing_model).start()
    Thread(target=run_hot_load_ocr_model).start()
