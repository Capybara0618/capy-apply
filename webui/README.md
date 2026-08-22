# Capybot Apply WebUI

这是 Capybot Apply 的 React 前端，只包含六个中文业务页面：总览、机会、任务、Agent、简历和导入。

## 本地开发

先在仓库根目录启动 FastAPI：

```powershell
uv run capybot apply serve --no-open
```

再启动 Vite：

```powershell
cd webui
npm ci
npm run dev
```

Vite 将 `/api` 和 `/ws` 代理到 `http://127.0.0.1:8765`。浏览器打开 Vite 输出的本地地址。

## 验证

```powershell
npm test -- --run
npm run build
npm audit --omit=dev
```

生产构建写入 `capybot/web/dist`，由 FastAPI 直接提供。Python sdist/wheel 构建钩子使用 `package-lock.json` 执行 `npm ci` 和 `npm run build`。

WebSocket 只接收 `apply.events` 失效通知，业务数据仍通过 `/api/apply/*` 重新读取；聊天正文和完整 Prompt 不经过 WebSocket。
