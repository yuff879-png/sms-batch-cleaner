# USB 短信批量清理工具

独立 Windows 桌面工具，通过 USB ADB 调用手机系统短信接口。最终流程为 **检测 → 确认 → 直接清空**，不备份短信，界面实时显示进度与日志，可一键复制排障。

[下载最新 Windows 便携版](https://github.com/yuff879-png/sms-batch-cleaner/releases/latest) · [更新记录](CHANGELOG.md)

## 使用与交付

正式更新源为 GitHub Releases。v1.1.0 起支持启动检查版本和手动“检查更新”；点击“下载并更新”后显示真实下载、校验与解压进度。独立安装窗口接管后再退出主窗口，更新只替换程序并保留 `data/`。v1.0.0 需要手动解压新版一次。维护者发布步骤见 [RELEASING.md](RELEASING.md)。

完整解压发行包后双击 `启动短信清理工具.cmd`，保留 `app` 和 `runtime` 文件夹。详见 [使用说明.txt](使用说明.txt)。使用者不需要 Python、Java 或 Android SDK；ADB 和官方 Python 运行时随包提供，手机 USB 调试、root 授权及必要的 Windows USB 驱动仍需完成。

目标环境为 Windows 10/11 x64、Android 10+ 主用户和可用 root。可连接多台后明确选择，也可逐台换机。操作与 DX、601、juhe-console 服务及部署独立。

## 文件与职责

| 文件 | 改动和目的 |
| --- | --- |
| app.py | 中文界面、设备选择、直接清空确认、异步进度、停止反馈、实时日志和一键复制 |
| version.py、update-channel.json | 统一版本号、公开更新源和更新验签公钥 |
| updater.py、update_worker.py | 签名和哈希校验、下载进度、独立安装窗口、失败恢复 |
| publish_release.py、RELEASING.md | 发布前校验、GitHub 草稿上传与正式发布流程 |
| core.py | ADB 绑定设备、只包含编号和指纹的临时计划、校验、错误分类、删除复查、异常状态锁 |
| android/SmsBridge.java | root 取得短信服务外部句柄后使用 phone 身份读取与删除；不安装 App、不修改全局权限 |
| android/build.ps1 | 编译最低 API 29 的 Java/Dex 桥接程序 |
| test_core.py、test_protocol.py、test_adb_errors.py | 验证错机、篡改、断流、缺失结果、停止、异常退出及 root 错误分类 |
| test_update_ui.py、test_updater.py、test_update_validation.py | 验证更新互斥、签名、解压、回滚、启动报告及接管进程身份 |
| build_release.py | 构建免安装 Windows 包，只纳入程序、依赖与说明，排除运行资料 |
| requirements-build.txt、.github/workflows/tests.yml | 固定验签依赖，并在 GitHub Windows 环境持续运行检查 |
| CHANGELOG.md | 记录正式版本的功能和修复，作为更新说明发布 |
| 使用说明.txt | 面向实际操作者的中文使用和故障反馈步骤 |
| .gitignore | 排除日志、测试机资料、构建产物和临时文件 |

## 数据与删除语义

检测只向电脑输出短信 ID、内容指纹和统计，不导出或保存短信正文。临时计划在正常清理完成、切机或退出时删除。异常退出可能留下编号与校验值，可重新检测生成新计划。

清理严格按本次计划的 ID 逐条调用系统短信接口，并检查实际返回和最终剩余数量。每条执行前检查指纹；已变化的记录在检查时跳过。Android 的单 ID 删除不接受原子指纹条件，读取与删除之间仍有很短的并发窗口，不保证这一窗口中的编辑也能被保护。检测后新增的其他 ID 不纳入本次计划。

采用单条 URI 是因为 Android 15 的会话删除入口有额外订阅过滤，在现场手机对旧记录返回 0；精确单 ID 接口已实测成功，并由系统更新会话。没有直接操作 SQLite，没有清空短信应用数据，没有修改 AppOps 或 SELinux。

范围为手机主用户本机 SMS，不含 MMS、SIM 卡记录、RCS 和云端。工具没有备份与恢复功能。日志只包含操作、数量、安全错误类别与阶段，设备序列号仅显示末尾。

跨进程手机锁避免工具同时操作同一设备。断线或异常退出导致结果未知时，电脑保存启动标识锁，手机重新启动后才允许再次操作。临时 root 在重启后可能失效，需操作者重新准备 root。

v1.1.3 对 `device_busy / operation_lock` 增加单独解释与只读诊断。Android 11 的 Java 文件锁接口可能把锁冲突和部分加锁权限错误都返回为未取得锁；因此不能仅凭该错误断言存在旧进程，也不能据此认定 root 不可用。失败时按当前锁文件的设备号和 inode 检查可见的内核锁记录，未观察到匹配项不代表没有持有者。仍使用原有操作锁，不删除、替换锁文件，不自动终止进程或更改系统安全策略。

该错误出现在获取短信服务之前，本次请求尚未读写短信；其他任务可能仍在运行。先确认原任务是否完成或停止，只保留一个操作窗口。持续失败时重启手机再检测；重启后仍报错，应提供完整日志进行兼容性排查。本版本改进错误分类和诊断，尚未在反馈此错误的 MIUI 12.5.7 / Android 11 手机上验证具体原因。

## 版本与兼容性

未 root 的手机不能使用本工具清理短信。检测到 `su` 缺失时显示“未检测到可用 root”，并记录 `ROOT_UNAVAILABLE`；USB 离线、未授权及普通文件操作错误分别分类。不得把缺少 root 误报为设备离线。

v1.1.2 起同时校验短信读取权限。Android 在读取 AppOp 被限制时可能返回空游标，因此读取权限未知或受限不能作为 0 条使用；删除前逐条查询和结束复查同样执行权限检查。日志记录安全的权限模式及设备尾号，不记录短信内容。此修复针对已确认的代码缺口，尚未在反馈残留消息的 Android 11 手机上验证原因。

检测到 0 条仅表示当前用户短信接口返回的本机 SMS 数量；Google 信息等应用还可能显示缓存会话或范围外消息。遇到不一致，先核对设备尾号、等待最终清理结果，再重新打开短信应用并检查会话内容。保留同一设备的完整检测/结果日志供排查，不通过清除应用数据来掩盖差异。

## 验证结果

2026-11-24：

- v1.1.0：未 root 的 Android 16 手机在线检测返回 `ROOT_UNAVAILABLE`，不再误报离线；未读写该手机短信。
- v1.1.0：本地自动测试覆盖更新界面互斥、签名、文件校验、异常下载、安装回滚和启动确认。
- v1.1.0：隔离便携目录中演练签名包校验、独立进度窗口接管、替换、验证和重新启动；模拟数据与设备锁文件保持一致。
- 以下短信删除验证属于 v1.0.0；v1.1.0 没有再次删除手机短信。

- 25 项主机安全与协议测试通过；包括无备份扫描断流时不产生可用计划、不保存正文。
- 界面编译、启动、操作状态测试和窗口可视检查通过。
- 实测手机为 23106RN0DA、Android 15、用户声明临时 root；读取 20 条收到短信。
- 经用户明确授权，只删除最早的 1 条：实际删除 1、目标剩余 0、手机总数 19；其余 19 条内容指纹保持一致。
- 实际永久 root 的小米 9A 尚未测试；没有把 Android 15 测试机等同于 9A。
- 大量短信吞吐未实测。50 万条为单次计划保护上限，不代表该规模的性能验收。

本地早期排查曾按当时需求生成测试机备份，位于开发目录 data/，未纳入最终程序或发行包。最终产品流程不生成短信备份。

## 更新机制来源

更新分阶段执行、独立进度窗口、签名清单和失败恢复的产品流程参考 [tcl-ad-tool](https://github.com/yuff879-png/tcl-ad-tool)。本项目使用 Python 实现，运行包不包含 GitHub 凭证或签名私钥。

## 开发构建

```powershell
python -m unittest discover -v
python -m py_compile core.py app.py build_release.py
python app.py --smoke-test
powershell -ExecutionPolicy Bypass -File android\build.ps1
python build_release.py --platform-tools C:\platform-tools
```

构建机使用 Python 3.14、JDK 21、Android SDK 36.1 / D8，Android 最低 API 29。第三方 ADB 声明保留在 `app/platform-tools/NOTICE.txt`。

便携包保留官方 Python 运行时的原始签名，不使用自制未签名 EXE，不要求关闭应用控制策略。Python/Tcl/Tk、cryptography 与 cffi 的许可随运行时一并保留。
