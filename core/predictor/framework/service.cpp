// Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "core/predictor/framework/service.h"
#ifdef BCLOUD
#include <base/time.h>  // butil::Timer
#else
#include <butil/time.h>  // butil::Timer
#endif

#include <inttypes.h>

#include <list>
#include <string>
#include <vector>

#include "core/predictor/common/constant.h"
#include "core/predictor/common/inner_common.h"
#include "core/predictor/framework/channel.h"
#include "core/predictor/framework/dag_view.h"
#include "core/predictor/framework/manager.h"
#include "core/predictor/framework/predictor_metric.h"  // PredictorMetric
#include "core/predictor/framework/resource.h"
#include "core/predictor/framework/server.h"

namespace baidu {
namespace paddle_serving {
namespace predictor {

int InferService::init(const configure::InferService& conf) {
  _infer_service_format = conf.name();

  std::string merger = conf.merger();
  if (merger == "") {
    merger = "default";
  }
  if (!MergerManager::instance().get(merger, _merger)) {
    LOG(ERROR) << "Failed get merger: " << merger;
    return ERR_INTERNAL_FAILURE;
  } else {
    LOG(WARNING) << "Succ get merger: " << merger
                 << " for service: " << _infer_service_format;
  }

  std::string request_field_key = conf.request_field_key();
  if (request_field_key == "") {
    request_field_key = "model_name";
  }
  _request_field_key = request_field_key;

  ServerManager& svr_mgr = ServerManager::instance();
  if (svr_mgr.add_service_by_format(_infer_service_format) != 0) {
    LOG(ERROR) << "Not found service by format name:" << _infer_service_format
               << "!";
    return ERR_INTERNAL_FAILURE;
  }

  // 创建映工作流射表
  if (_request_to_workflow_map.init(
          MAX_WORKFLOW_NUM_IN_ONE_SERVICE /*load_factor=80*/) != 0) {
    LOG(ERROR) << "init request to workflow map failed, bucket_count["
               << MAX_WORKFLOW_NUM_IN_ONE_SERVICE << "].";
    return ERR_INTERNAL_FAILURE;
  }
  // 读取infer service workflow
  uint32_t flow_size = conf.workflows_size();
  for (uint32_t fi = 0; fi < flow_size; fi++) {
    const std::string& workflow_name = conf.workflows(fi);
    Workflow* workflow = WorkflowManager::instance().item(workflow_name);
    if (workflow == NULL) {
      LOG(ERROR) << "Failed get workflow by name:" << workflow_name;
      return ERR_INTERNAL_FAILURE;
    }
    std::vector<Workflow*> workflows;
    workflow->regist_metric(full_name());
    workflows.push_back(workflow);
    // 放入brpc服务容器
    if (_request_to_workflow_map.insert(workflow_name, workflows) == NULL) {
      LOG(ERROR) << "insert [" << workflow_name
                 << "] to _request_to_workflow_map failed.";
      return ERR_INTERNAL_FAILURE;
    }
    _flows.push_back(workflow_name);
    LOG(INFO) << "Succ insert workflow[" << workflow_name
              << "], to _request_to_workflow_map.";
  }

  if (_flows.size() == 0) {
    LOG(INFO) << "Load infer_service: " << _infer_service_format << " failed!";
    return ERR_INTERNAL_FAILURE;
  }

  LOG(INFO) << "Succ load infer_service: " << _infer_service_format << "!";
  return ERR_OK;
}

int InferService::reload() { return ERR_OK; }

const std::string& InferService::name() const { return _infer_service_format; }

// ´®ÐÐÖ´ÐÐÃ¿¸öworkflow
int InferService::inference(const google::protobuf::Message* request,
                            google::protobuf::Message* response,
                            const uint64_t log_id,
                            butil::IOBufBuilder* debug_os) {
  TRACEPRINTF("(logid=%" PRIu64 ") start to inference", log_id);

  // when funtion call begins, framework will reset
  // thread local variables&resources automatically.
  if (Resource::instance().thread_clear() != 0) {
    LOG(ERROR) << "(logid=" << log_id << ") Failed thread clear whole resource";
    return ERR_INTERNAL_FAILURE;
  }

  TRACEPRINTF("(logid=%" PRIu64 ") finish to thread clear", log_id);

  //  if (_enable_map_request_to_workflow) {
  VLOG(2) << "(logid=" << log_id << ") enable map request == True";
  std::vector<Workflow*>* workflows = _map_request_to_workflow(request, log_id);
  if (!workflows || workflows->size() == 0) {
    LOG(ERROR) << "(logid=" << log_id << ") Failed to map request to workflow";
    return ERR_INTERNAL_FAILURE;
  }
  size_t fsize = workflows->size();
  for (size_t fi = 0; fi < fsize; ++fi) {
    Workflow* workflow = (*workflows)[fi];
    if (workflow == NULL) {
      LOG(ERROR) << "(logid=" << log_id
                 << ") Failed to get valid workflow at: " << fi;
      return ERR_INTERNAL_FAILURE;
    }
    TRACEPRINTF("(logid=%" PRIu64 ") start to execute workflow[%s]",
                log_id,
                workflow->name().c_str());
    int errcode =
        _execute_workflow(workflow, request, response, log_id, debug_os);
    TRACEPRINTF("(logid=%" PRIu64 ") finish to execute workflow[%s]",
                log_id,
                workflow->name().c_str());
    if (errcode < 0) {
      LOG(ERROR) << "(logid=" << log_id << ") Failed execute workflow["
                 << workflow->name() << "] in:" << name();
      return errcode;
    }
    return ERR_OK;
  }

  int InferService::debug(const google::protobuf::Message* request,
                          google::protobuf::Message* response,
                          const uint64_t log_id,
                          butil::IOBufBuilder* debug_os) {
    return inference(request, response, log_id, debug_os);
  }

  int InferService::_execute_workflow(Workflow * workflow,
                                      const google::protobuf::Message* request,
                                      google::protobuf::Message* response,
                                      const uint64_t log_id,
                                      butil::IOBufBuilder* debug_os) {
    butil::Timer workflow_time(butil::Timer::STARTED);
    // create and submit beginer channel
    BuiltinChannel req_channel;
    req_channel.init(0, START_OP_NAME);
    req_channel = request;

    DagView* dv = workflow->fetch_dag_view(full_name(), log_id);
    dv->set_request_channel(req_channel, log_id);

    // call actual inference interface
    int errcode = dv->execute(log_id, debug_os);
    if (errcode < 0) {
      LOG(ERROR) << "(logid=" << log_id
                 << ") Failed execute dag for workflow:" << workflow->name();
      return errcode;
    }

    TRACEPRINTF("(logid=%" PRIu64 ") finish to dv execute", log_id);
    // create ender channel and copy
    const Channel* res_channel = dv->get_response_channel(log_id);
    if (res_channel == NULL) {
      LOG(ERROR) << "(logid=" << log_id << ") Failed get response channel";
      return ERR_INTERNAL_FAILURE;
    }

    if (!_merger || !_merger->merge(res_channel->message(), response)) {
      LOG(ERROR) << "(logid=" << log_id
                 << ") Failed merge channel res to response";
      return ERR_INTERNAL_FAILURE;
    }
    TRACEPRINTF("(logid=%" PRIu64 ") finish to copy from", log_id);

    workflow_time.stop();
    LOG(INFO) << "(logid=" << log_id
              << ") workflow total time: " << workflow_time.u_elapsed();
    PredictorMetric::GetInstance()->update_latency_metric(
        WORKFLOW_METRIC_PREFIX + dv->full_name(), workflow_time.u_elapsed());

    // return tls data to object pool
    workflow->return_dag_view(dv);
    TRACEPRINTF("(logid=%" PRIu64 ") finish to return dag view", log_id);
    return ERR_OK;
  }

  std::vector<Workflow*>* InferService::_map_request_to_workflow(
      const google::protobuf::Message* request, const uint64_t log_id) {
    const google::protobuf::Descriptor* desc = request->GetDescriptor();
    const google::protobuf::FieldDescriptor* field =
        desc->FindFieldByName(_request_field_key);
    if (field == NULL) {
      LOG(ERROR) << "(logid=" << log_id << ") No field[" << _request_field_key
                 << "] in [" << desc->full_name() << "].";
      return NULL;
    }
    if (field->is_repeated()) {
      LOG(ERROR) << "(logid=" << log_id << ") field[" << desc->full_name()
                 << "." << _request_field_key << "] is repeated.";
      return NULL;
    }
    if (field->cpp_type() !=
        google::protobuf::FieldDescriptor::CPPTYPE_STRING) {
      LOG(ERROR) << "(logid=" << log_id << ") field[" << desc->full_name()
                 << "." << _request_field_key << "] should be string";
      return NULL;
    }
    const std::string& field_value =
        request->GetReflection()->GetString(*request, field);

    std::vector<Workflow*>* p_workflow;
    if (field_value == "") {
      const std::string& first_workflow_name = flows[0];
      p_workflow = _request_to_workflow_map.seek(first_workflow_name);
      LOG(INFO) << "(logid=" << log_id << ") " << desc->full_name() << ",field"
                << _request_field_key << " : " << first_workflow_name;
    } else {
      p_workflow = request_to_workflow_map.seek(field_value);
      LOG(INFO) << "(logid=" << log_id << ") " << desc->full_name() << ",field"
                << _request_field_key << " : " << field_value;
    }
    if (p_workflow == NULL) {
      LOG(ERROR) << "(logid=" << log_id << ") cannot find key[" << field_value
                 << "] in _request_to_workflow_map";
      return NULL;
    }
    return p_workflow;
  }

}  // namespace predictor
}  // namespace paddle_serving
}  // namespace baidu
