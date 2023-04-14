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

#pragma once
#include <string>
#include <unordered_set>
#include <utility>

#include "core/predictor/common/constant.h"
#include "core/predictor/common/inner_common.h"
#include "core/predictor/framework/service.h"
#include "core/predictor/framework/workflow.h"

namespace baidu {
namespace paddle_serving {
namespace predictor {

using configure::InferServiceConf;
using configure::WorkflowConf;

class Workflow;
// class InferService;
// class ParallelInferService;

template <typename I>
I* create_item_impl() {
  return new (std::nothrow) I();
}

template <>
inline InferService* create_item_impl<InferService>() {
  if (FLAGS_use_parallel_infer_service) {
    return new (std::nothrow) ParallelInferService();
  } else {
    return new (std::nothrow) InferService();
  }
}

class WorkflowManager {
 public:
  static WorkflowManager& instance() {
    static WorkflowManager mgr;
    return mgr;
  }

  int load_workflows(bool mem_merge = false) {
    WorkflowConf workflow_conf;
    if (configure::read_proto_conf(
            _workflow_path, _workflow_file, &workflow_conf) != 0) {
      LOG(ERROR) << "Failed load manager<" << Workflow::tag()
                 << "> configure from " << _workflow_path << "/"
                 << _workflow_file;
      return -1;
    }
    // 当前配置文件读取的工作流名称
    std::unordered_set<std::string> new_workflow_set;
    auto insert_workflows = [this, workflow_conf, &new_workflow_set]() -> int {
      LOG(ERROR) << "4444444444444444444";
      try {
        uint32_t item_size = workflow_conf.workflows_size();
        for (uint32_t ii = 0; ii < item_size; ii++) {
          std::string name = workflow_conf.workflows(ii).name();
          new_workflow_set.insert(name);

          // 忽略已经初始化的工作流
          if (this->_item_map.find(name) != this->_item_map.end()) {
            LOG(WARNING) << "Proc inserted workflow name: " << name;
            continue;
          }

          Workflow* item = new (std::nothrow) Workflow();
          if (item == NULL) {
            LOG(ERROR) << "Failed create " << Workflow::tag()
                       << " for: " << name;
            return -1;
          }
          if (item->init(workflow_conf.workflows(ii)) != 0) {
            LOG(ERROR) << "Failed init item: " << name << " at:" << ii << "!";
            return -1;
          }
          LOG(ERROR) << "5555555555555555555555";
          std::pair<
              typename boost::unordered_map<std::string, Workflow*>::iterator,
              bool>
              r = this->_item_map.insert(std::make_pair(name, item));
          if (!r.second) {
            LOG(ERROR) << "Failed insert item:" << name << " at:" << ii << "!";
            return -1;
          }
          LOG(ERROR) << "66666666666666666666666";
          LOG(INFO) << "Succ init item:" << name
                    << " from conf:" << this->_workflow_path << "/"
                    << this->_workflow_file << ", at:" << ii << "!";
        }
        return 0;
      } catch (...) {
        LOG(ERROR) << "Config[" << this->_workflow_path << "/"
                   << this->_workflow_file << "] format "
                   << "invalid, load failed";
        return -1;
      }
    };
    if (mem_merge) {
      LOG(ERROR) << "111111111111111111111111";
      if (insert_workflows() != 0) {
        return -1;
      }
      LOG(ERROR) << "2222222222222222222222222";
      typename boost::unordered_map<std::string, Workflow*>::iterator it =
          _item_map.begin();
      for (; it != _item_map.end(); ++it) {
        std::string workflow_name = it->first;
        if (new_workflow_set.find(workflow_name) == new_workflow_set.end()) {
          LOG(ERROR) << "33333333333333333333333";
          // 擦除不存在的工作流
          delete (it->second);
          it->second = nullptr;
          _item_map.erase(it++);
        } else {
          LOG(ERROR) << "44444444444444444444444";
          it++;
        }
      }
      return 0;
    } else {
      LOG(ERROR) << "0000000000000000000000";
      return insert_workflows();
    }
  }

  int initialize(const std::string path, const std::string file) {
    _workflow_path = path;
    _workflow_file = file;
    load_workflows(false);
  }

  Workflow* create_item() { return create_item_impl<Workflow>(); }

  Workflow* item(const std::string& name) {
    typename boost::unordered_map<std::string, Workflow*>::iterator it;
    it = _item_map.find(name);
    if (it == _item_map.end()) {
      LOG(WARNING) << "Not found item: " << name << "!";
      return NULL;
    }

    return it->second;
  }

  Workflow& operator[](const std::string& name) {
    Workflow* i = item(name);
    if (i == NULL) {
      std::string err = "Not found item in manager for:";
      err += name;
      throw std::overflow_error(err);
    }
    return *i;
  }

  int reload() {
    // 重载工作流
    if (load_workflows(true) != 0) {
      LOG(ERROR) << "Reload workflows file path:"
                 << " at: [" << _workflow_path << "/" << _workflow_file
                 << "] failed!";
    }

    int ret = 0;
    typename boost::unordered_map<std::string, Workflow*>::iterator it =
        _item_map.begin();
    for (; it != _item_map.end(); ++it) {
      if (it->second->reload() != 0) {
        LOG(WARNING) << "failed reload item: " << it->first << "!";
        ret = -1;
      }
    }

    LOG(INFO) << "Finish reload " << _item_map.size() << " " << Workflow::tag()
              << "(s)";
    return ret;
  }

  int finalize() { return 0; }

 private:
  WorkflowManager() {}

 private:
  boost::unordered_map<std::string, Workflow*> _item_map;

  std::string _workflow_path;

  std::string _workflow_file;
};

class InferServiceManager {
 public:
  static InferServiceManager& instance() {
    static InferServiceManager mgr;
    return mgr;
  }

  int initialize(const std::string path, const std::string file) {
    InferServiceConf infer_service_conf;
    if (configure::read_proto_conf(path, file, &infer_service_conf) != 0) {
      LOG(ERROR) << "Failed load manager<" << InferService::tag()
                 << "> configure!";
      return -1;
    }

    try {
      uint32_t item_size = infer_service_conf.services_size();
      for (uint32_t ii = 0; ii < item_size; ii++) {
        std::string name = infer_service_conf.services(ii).name();
        InferService* item = new (std::nothrow) InferService();
        if (item == NULL) {
          LOG(ERROR) << "Failed create " << InferService::tag()
                     << " for: " << name;
          return -1;
        }
        if (item->init(infer_service_conf.services(ii)) != 0) {
          LOG(ERROR) << "Failed init item: " << name << " at:" << ii << "!";
          return -1;
        }

        std::pair<
            typename boost::unordered_map<std::string, InferService*>::iterator,
            bool>
            r = _item_map.insert(std::make_pair(name, item));
        if (!r.second) {
          LOG(ERROR) << "Failed insert item:" << name << " at:" << ii << "!";
          return -1;
        }

        LOG(INFO) << "Succ init item:" << name << " from conf:" << path << "/"
                  << file << ", at:" << ii << "!";
      }
    } catch (...) {
      LOG(ERROR) << "Config[" << path << "/" << file << "] format "
                 << "invalid, load failed";
      return -1;
    }
    return 0;
  }

  InferService* create_item() { return create_item_impl<InferService>(); }

  InferService* item(const std::string& name) {
    typename boost::unordered_map<std::string, InferService*>::iterator it;
    it = _item_map.find(name);
    if (it == _item_map.end()) {
      LOG(WARNING) << "Not found item: " << name << "!";
      return NULL;
    }

    return it->second;
  }

  InferService& operator[](const std::string& name) {
    InferService* i = item(name);
    if (i == NULL) {
      std::string err = "Not found item in manager for:";
      err += name;
      throw std::overflow_error(err);
    }
    return *i;
  }

  int reload() {
    int ret = 0;
    typename boost::unordered_map<std::string, InferService*>::iterator it =
        _item_map.begin();
    for (; it != _item_map.end(); ++it) {
      if (it->second->reload() != 0) {
        LOG(WARNING) << "failed reload item: " << it->first << "!";
        ret = -1;
      }
    }

    LOG(INFO) << "Finish reload " << _item_map.size() << " "
              << InferService::tag() << "(s)";
    return ret;
  }

  int finalize() { return 0; }

 private:
  InferServiceManager() {}

 private:
  boost::unordered_map<std::string, InferService*> _item_map;
};

}  // namespace predictor
}  // namespace paddle_serving
}  // namespace baidu
