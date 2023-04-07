from __future__ import print_function
import logging
import grpc

import sys

sys.path.append("proto")
sys.path.append("proto")
from proto import model_hot_load_service_pb2_grpc
from proto import model_hot_load_service_pb2


def run():
    with grpc.insecure_channel('localhost:9099') as channel:
        stub = model_hot_load_service_pb2_grpc.HotLoadModelServiceStub(channel)
        response = stub.loading(model_hot_load_service_pb2.Request(
            model_name='uci_housing_model',
            model_file_address='http://localhost:8080/uci_housing_model.tar.gz',
            is_remote=True,
            is_tar_packed=True,
            timeout=300
        ))
    print(f'code: {response.err_no}, msg: {response.err_msg}')


# 测试一下
if __name__ == '__main__':
    logging.basicConfig()
    run()
