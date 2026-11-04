# 第一次上传 GitHub

建议仓库名称：`maibot-personal-relationship-plugin`。

仓库简介可直接复制：

> MaiBot 个人自用好感度插件：按真实账号维护分级关系，支持自动评分、手动授权、WebUI 管理，以及规划器与回复器的关系设定注入。

## 用网页上传，不需要命令行

1. 登录 [GitHub](https://github.com)，右上角点 **＋**，选择 **New repository**（新建仓库）。
2. **Repository name**（仓库名称）填 `maibot-personal-relationship-plugin`。
3. **Description**（简介）粘贴上面的中文简介。
4. 选择 **Public**（公开）。这表示任何人都能看到源码。
5. 不勾选自动生成 README、`.gitignore` 或许可证；发布目录已准备好这些文件。
6. 点击 **Create repository**（创建仓库）。
7. 新仓库页面找到 **uploading an existing file**（上传已有文件）；如果已经有文件，点 **Add file → Upload files**（添加文件 → 上传文件）。
8. 打开整理好的发布目录，把目录里面的文件和 `docs`、`tests` 两个子目录拖到网页上传区。上传的是目录内的内容，不是外面那层大文件夹，也不是 ZIP 文件。
9. 等待上传完成，下面 **Commit changes**（提交更改）写“首次发布个人自用好感度插件”，再点绿色提交按钮。
10. 打开仓库首页，确认 README 能显示，并且根目录可看到 `_manifest.json` 和 `plugin.py`。

不要从正在运行的插件目录直接拖文件。请使用本次整理的发布目录，它没有你的真实配置、数据库和旧 Git 历史。

## 上传后可以设置

仓库首页右侧 **About**（关于）旁边的齿轮，可以修改中文简介与主题标签。建议标签：`maibot`、`plugin`、`qq-bot`、`relationship`。

以后修改文件，可继续 **Add file → Upload files** 上传同名文件覆盖。每次提交都应简单说明改了什么。不要在截图、Issue 或日志里公开 API Key 和 QQ 私聊内容。

## 如果由助手代为上传

只需在本机完成 GitHub 的官方登录流程。不要在对话里发送密码、验证码或访问令牌。没有已登录的 GitHub 工具时，助手无法凭用户名上传；用户名仅用于公开署名和仓库链接。
