# chrome 插件交互精简(账号管理器化)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps 用 checkbox。

**Goal:** 插件精简为"我的账号管理器":只留 apikey 录入 + 账号列表 + 点卡注入开无痕 + 无痕登录采集 + per-card 验活;移除当前页 cookie 采集与当前标签页状态展示。

**Architecture:** 纯前端删减 + 收敛。保留 service-worker 三条无痕流程(`openAccountSession`/`startRemoteLogin`/账号验活),删掉当前页采集链路(`syncCurrentSession`/`collectXHSCookies`/`checkLoginStatus`)及 popup 对应 UI。

**Tech Stack:** Chrome MV3 扩展(popup HTML/CSS/JS + background service-worker module)。

**Spec:** `docs/design/2026-07-15-extension-account-manager-design.md`

## Global Constraints

- 全中文注释无 emoji;UI 文案禁 emoji(SVG 图标);commit `type(scope): 描述`,显式列文件禁 `git add -A`。
- **权限默认保留**:`webRequest` 极可能被 `startRemoteLogin` 采 cookie 依赖,`cookies` 被注入/无痕采集依赖——
  除非逐一证实某权限**仅**被已删的当前页采集用到,否则不删(误删权限静默废掉无痕流程)。
- 无自动化测试:每步后靠 `chrome://extensions` load-unpacked 手工走查(见 Task 末验证清单)。
- 保留:`openAccountSession`(点卡注入开无痕)、`startRemoteLogin`(无痕登录采集)、`fetchAccountCookies`、
  `pushCookies`、账号验活轮询、无痕权限 null 守卫、storage 回传终态机制。

---

### Task 1: 精简 popup + service-worker + 版本号(整体重做,一并审)

**Files:**
- Modify: `chrome-extension/popup/popup.html`(删状态指示 / 用户信息区 / 同步按钮 / 打开小红书按钮;server-url 折叠)
- Modify: `chrome-extension/popup/popup.js`(删 `syncAccount`/`checkLoginStatus`/`getUserInfo` 及监听/元素引用)
- Modify: `chrome-extension/background/service-worker.js`(删 `syncCurrentSession`/`collectXHSCookies`/`checkLoginStatus` 及其 message 分派)
- Modify: `chrome-extension/popup/popup.css`(清理被删元素样式)
- Modify: `chrome-extension/manifest.json`(version `2.0.4` → `2.1.0`)

**Interfaces:**
- Consumes(保留不动):service-worker 的 `openAccountSession(accountId)` / `startRemoteLogin()` /
  `fetchAccountCookies(accountId)` / 验活端点调用;popup 的账号列表渲染 + 点卡 + per-card 检测轮询。
- Produces:精简后 popup 仅 5 功能;service-worker 无当前页采集分派。

- [ ] **Step 1: 先读三文件确认删/留边界**

读 `popup/popup.html`、`popup/popup.js`、`background/service-worker.js`,逐一标注:
- 删:`#status-indicator` / `.status-text` / `#cookie-count` / `#user-info-section`(头像/昵称/user-id)、
  `#btn-sync`(同步当前账号)、`#btn-open-xhs`(打开小红书)及其 popup.js 事件绑定 + 元素引用 +
  `syncAccount`/`checkLoginStatus`/`getUserInfo` 函数 + 相关 `chrome.storage.onChanged` 分支(仅当前页态用的)。
- 留:`#apikey` + 保存、`#server-url`(改折叠)、`#accounts-list` + `#btn-refresh-accounts`、
  点卡 → openAccountSession、per-card「检测」、`#btn-remote-login`(无痕登录采集)、`#message`、帮助。
- service-worker 删:`syncCurrentSession` / `collectXHSCookies` / `checkLoginStatus` 及 message router 里
  对应 `case`/分支(如 `collectCookies` / `syncCurrentSession` action)。留其余全部无痕流程函数。

- [ ] **Step 2: 改 popup.html**

- 删 `#status-indicator`/`.status-text`/`#cookie-count` 整块、`#user-info-section` 整块、`#btn-sync`、`#btn-open-xhs`。
- server-url 折叠:把 `#server-url` 那行包进 `<details><summary>高级设置</summary> ... </details>`,
  `placeholder="https://mcp.nbdpsy.com"` 保留;apikey 行留在主区。
- 保留 `#accounts-list`/`#btn-refresh-accounts`/`#btn-remote-login`(文案可改「打开隐私窗口登录(加/换号)」)/`#message`/帮助。

- [ ] **Step 3: 改 popup.js**

- 删元素引用 `statusIndicator`/`statusText`/`cookieCount`/user-info 相关/`btnSync`/`btnOpenXhs`。
- 删函数 `syncAccount`、`checkLoginStatus`、`getUserInfo`,及 `DOMContentLoaded` 里对它们的调用与
  `#btn-sync`/`#btn-open-xhs` 的 `addEventListener`;删只服务于当前页态的 `chrome.storage.onChanged` 分支
  (保留 `remoteLoginResult`/`accountSessionResult` 回传分支)。
- `loadConfig` 保留(读 server-url + apikey);启动流程改为:loadConfig → 若有 apikey 直接渲染账号列表
  (删原先"先 checkLoginStatus 检测当前页"的启动路径)。
- 删除后产生的 unused import/变量一并清(仅清本次删动产生的孤儿)。

- [ ] **Step 4: 改 service-worker.js**

- 删 `syncCurrentSession` / `collectXHSCookies` / `checkLoginStatus` 三函数,及 `onMessage` 路由里对应
  action 分支(popup 已不再发这些消息)。
- 保留 `openAccountSession` / `startRemoteLogin` / `fetchAccountCookies` / `pushCookies` / `injectCookiesIntoStore` /
  `finishRemoteLogin` / `finishAccountSession` / 验活 / 无痕权限指引常量与 null 守卫,全部不动。
- 若 `pushCookies` 仅被已删的 `syncCurrentSession` 调用而 `startRemoteLogin` 不用,则它变孤儿——
  **核实**:`startRemoteLogin` 采集后也走 `/api/cookies/import`(经 `pushCookies` 或直连 fetch)?
  是则保留;确证无任何保留流程调用才删。宁留勿误删。

- [ ] **Step 5: 清 popup.css + 升版本**

- 删被移除元素的样式块(`.status-indicator`/`.user-info-section`/`.cookie-count` 等),
  轻度统一间距,不重写视觉体系。
- `manifest.json` version → `2.1.0`。

- [ ] **Step 6: 手工走查(load-unpacked)**

`chrome://extensions` → 重新加载解包插件 → 逐条验:
1. 主界面只露 apikey(+ 折叠的高级设置),无状态指示/用户信息/同步当前账号/打开小红书入口。
2. 填 apikey 保存 → 「我的账号」列出归属账号 + cookie_status 徽标。
3. 点一张卡 → 开无痕窗 + 注入 + 小红书已登录态。
4. 点「打开隐私窗口登录」→ 无痕人工登录 → 采集成功 → 后台新增/更新账号(回后台或列表可见)。
5. 点某卡「检测」→ 轮询到有效/失效/异常,徽标更新;error 态标「非 cookie 失效」。
6. 控制台无 `undefined function` / 缺元素报错(删干净了引用)。

- [ ] **Step 7: 提交**

```bash
git add chrome-extension/popup/popup.html chrome-extension/popup/popup.js \
  chrome-extension/background/service-worker.js chrome-extension/popup/popup.css \
  chrome-extension/manifest.json
git commit -m "feat(extension): 交互精简为账号管理器(v2.1.0)——删当前页采集/状态区,留 apikey+账号列表+无痕注入/登录/验活"
```

## Self-Review 对照

- 五条"只需要"全保留 → Step 2-4 保留清单 ✓
- 三条"不需要"移除(当前页采集/当前标签态/打开小红书)→ Step 2-4 删清单 ✓
- 权限不误删 → Global Constraints + Step 4 核实 ✓
- 无痕三流程回归 → Step 6 手工验 3/4/5 ✓
- 版本号 → Step 5 ✓
