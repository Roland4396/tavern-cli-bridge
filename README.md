# tavern-cli-bridge

原生 CLI 桥、兼容路由和独立 Unix 套接字网关。

包括 Carbon 的上游非流式保活，以及 CLI system 的 XML 定义和原位生效标记。仅提供程序与禁用状态的示例，不包含官方可执行文件、完整供应商内部目录或任何登录状态。

先运行 `python3 tools/build_fixture.py` 生成空白合成测试库，执行 `PYTHONPATH=src python3 -m unittest discover -s tests -p test_system_timeline.py`。真实运行前请自行取得对应的、获授权使用的固定版本官方二进制（放入 vendor/antigravity），并执行 python3 tools/import_model_enums.py /本地/受信任的枚举映射.json。只接受版本匹配的静态 MODEL 名称→整数映射，不接收账户、目录响应或 token。生成的映射不受 Git 管理。配置、密钥和账号状态均在仓库外提供；复制示例配置后设为 0600，分别生成独立密钥。构建目标 app 或 gateway。生产诊断录制挂钩不包含在发行版中。
