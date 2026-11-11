# Project Path Migration

The workspace project directory is `E:\deepseek_dsd\weixin\dsh-wechat-bridge-bot`.

The desktop DSH profile loads the local plugin as `dsh-wechat-bridge`. Its dependency link must target the workspace directory above. After renaming the project directory, update all three profile references together:

- `C:\Users\Administrator\.dsh\profiles\desktop\package.json`
- `C:\Users\Administrator\.dsh\profiles\desktop\pnpm-lock.yaml`
- `C:\Users\Administrator\.dsh\profiles\desktop\node_modules\dsh-wechat-bridge` symbolic link

The package name, plugin slug, HTTP route prefix, and public repository URL remain `dsh-wechat-bridge`; they are identifiers, not workspace directory names. Restart DSH after changing the profile link so the plugin is discovered from the new location.
