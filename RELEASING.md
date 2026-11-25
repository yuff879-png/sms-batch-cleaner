# 发布与在线更新

运行包无需 GitHub 账号或令牌。更新源和验签公钥在 `update-channel.json`，签名私钥保存在维护电脑的仓库外。私钥、手机数据及日志不得提交或上传。

更新流程：启动检查稳定版本 → 用户确认下载 → 实时下载进度 → 校验签名与 SHA-256 → 解压 → 独立更新窗口接管 → 退出旧程序 → 替换程序文件 → 验证启动 → 打开新版。清理短信时禁止更新，更新后不自动继续删除。

## 维护者发布步骤

1. 修改源码并更新 `version.py`、`CHANGELOG.md` 和使用说明。
2. 运行 `python -m unittest discover -v`，检查界面和受影响功能。
3. Android 桥接有修改时执行 `powershell -ExecutionPolicy Bypass -File android\build.ps1`。
4. 执行 `python build_release.py`，默认从用户目录 `.codex/release-keys/sms-batch-cleaner-ed25519.pem` 读取私钥。其他路径使用 `--signing-key`。运行时使用官方 Python 签名文件，cryptography 仅用于更新验签。
5. 提交本项目源码，创建与 `version.py` 一致的轻量标签 `v版本号`，推送分支和标签。
6. 先运行 `python publish_release.py` 复查资产，再运行 `python publish_release.py --publish`。该操作先建立草稿、上传完整资产，最后公开发行。
7. 从 GitHub 重新下载清单和更新包验签、验哈希，再在测试目录执行更新演练。确认界面版本和远端标签一致。

`publish_release.py` 通过本机 Git Credential Manager 获取当前操作者授权，不将令牌写入文件或传给最终使用者。上传失败时 Release 保留为草稿，请处理该草稿后重试，避免重复版本。

初始 v1.0.0 没有在线更新入口，需手动解压 v1.1.0 一次；之后可在界面检查并安装更新。系统策略阻止的文件不会通过关闭安全设置处理。

## Android 锁诊断测试

修改锁诊断解析后，先按上述步骤构建桥接器，再运行实际嵌套解析器的 JVM 测试。测试输出独立存放，不能放进 `android/build/classes` 或发行桥接包。

```powershell
& 'C:\Program Files\Java\jdk-21.0.10\bin\javac.exe' -encoding UTF-8 --release 8 -classpath android\build\classes -d android\build\testclasses android\tests\LockObservationTest.java
if ($LASTEXITCODE -ne 0) { throw 'JVM test compilation failed' }
& 'C:\Program Files\Java\jdk-21.0.10\bin\java.exe' -cp 'android\build\classes;android\build\testclasses' LockObservationTest
if ($LASTEXITCODE -ne 0) { throw 'JVM tests failed' }
```

这些测试不依赖手机，覆盖设备号及 inode 匹配、锁类型、等待者、读取失败和读取大小限制；不能代替现场 ROM 验证。
