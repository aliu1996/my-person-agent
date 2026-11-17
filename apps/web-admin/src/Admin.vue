<script setup>
import { computed, ref } from 'vue'
import { ElMessageBox } from 'element-plus'

const jwt = ref('')
const username = ref('')
const password = ref('')
const totp = ref('')
const setup = ref(null)
const busy = ref(false)
const error = ref('')
const result = ref('')
const tab = ref('provider')
const rows = ref([])
const editor = ref(null)
const page = ref(1)

const tabs = {
  provider: { title: '供应商', path: '/provider/list', columns: [['name', '名称'], ['providerType', '类型'], ['baseUrl', '地址'], ['protocol', '协议']] },
  model: { title: '模型', path: '/model/list', columns: [['displayName', '名称'], ['modelCode', '模型代码'], ['providerId', '供应商'], ['status', '状态']] },
  moderation: { title: '审核', path: '/moderation/page', columns: [['direction', '方向'], ['hitLayer', '规则'], ['snippet', '内容片段'], ['reviewStatus', '状态']] },
  user: { title: '用户', path: '/user/page', columns: [['id', '用户 ID'], ['phone', '手机号'], ['nickname', '昵称'], ['balance', '积分'], ['status', '状态']] },
  audit: { title: '审计', path: '/auditLog/page', columns: [['action', '操作'], ['targetType', '类型'], ['targetId', '对象'], ['createdAt', '时间']] },
}
const current = computed(() => tabs[tab.value])
const paginated = computed(() => ['moderation', 'user', 'audit'].includes(tab.value))

async function api(path, method = 'GET', body) {
  const response = await fetch('/api/admin' + path, {
    method, headers: { ...(jwt.value && { Authorization: 'Bearer ' + jwt.value }),
      ...(body !== undefined && { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok || data.code !== 0) {
    if (response.status === 401 && jwt.value) jwt.value = ''
    throw new Error(data.message || '管理服务请求失败')
  }
  return data.data
}

async function run(task) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  result.value = ''
  try { await task() } catch (issue) {
    if (issue !== 'cancel' && issue !== 'close') error.value = issue.message || '操作失败'
  } finally { busy.value = false }
}

async function load() {
  const value = await api(current.value.path + (paginated.value ? '?page=' + page.value : ''))
  rows.value = Array.isArray(value) ? value : value.items
}

async function login() {
  await run(async () => {
    const data = setup.value ? await api('/auth/bindTotp', 'POST', {
      setupToken: setup.value.setupToken, totpCode: totp.value,
    }) : await api('/auth/login', 'POST', {
      username: username.value, password: password.value, ...(totp.value && { totpCode: totp.value }),
    })
    password.value = ''
    totp.value = ''
    if (data.requireTotpSetup) { setup.value = data; return }
    setup.value = null
    jwt.value = data.adminJwt
    await load()
  })
}

function openEdit(group, row) {
  let value
  if (group === 'provider') {
    value = row ? {
      id: row.id, name: row.name, providerType: row.providerType, protocol: row.protocol,
      baseUrl: row.baseUrl, timeoutMs: row.timeoutMs, rpmLimit: row.rpmLimit, isEnabled: row.isEnabled,
    } : { name: '', providerType: 'cloud', protocol: 'openai', baseUrl: '', keys: [{ apiKey: '' }] }
  } else {
    const { id, ...fields } = row || {}
    value = row ? fields : {
      modelCode: '', providerId: null, displayName: '', upstreamModel: '',
      rateIn: 1, rateCache: 0, rateOut: 1,
    }
  }
  editor.value = { title: row ? '编辑' : '新增', path: '/' + group + (row ? '/edit' : '/add'),
    method: row ? 'PUT' : 'POST', text: JSON.stringify(value, null, 2) }
}

async function configure(path, title) {
  await run(async () => {
    editor.value = { title, path, method: 'PUT', text: JSON.stringify(await api(path), null, 2) }
  })
}

async function save() {
  await run(async () => {
    const command = editor.value
    const data = await api(command.path, command.method, JSON.parse(command.text))
    editor.value = null
    await load()
    result.value = JSON.stringify(data, null, 2)
  })
}

async function action(kind, row) {
  await run(async () => {
    if (kind === 'provider') {
      result.value = JSON.stringify(await api('/provider/' + row.id + '/test', 'POST'), null, 2)
    } else if (kind === 'chat') {
      const { value } = await ElMessageBox.prompt('输入测试提示词', '在线试聊')
      result.value = JSON.stringify(await api('/model/testChat', 'POST',
        { modelCode: row.modelCode, prompt: value }), null, 2)
    } else if (kind === 'impact') {
      result.value = JSON.stringify(await api('/model/' + encodeURIComponent(row.modelCode) + '/impact'), null, 2)
    } else if (kind === 'ban') {
      const status = row.status === 'active' ? 'banned' : 'active'
      await ElMessageBox.confirm(status === 'banned' ? '确认封禁该用户？' : '确认解封该用户？')
      await api('/user/' + row.id + '/ban', 'POST', { status })
      await load()
    } else if (kind === 'grant') {
      const { value } = await ElMessageBox.prompt('输入赠送的积分，1–100000', '赠送积分',
        { inputPattern: /^[0-9]{1,6}$/ })
      await api('/user/' + row.id + '/points', 'POST',
        { requestId: crypto.randomUUID(), points: Number(value) })
      await load()
    } else {
      await api('/moderation/' + row.id + '/review', 'POST', { decision: kind })
      await load()
    }
  })
}

function go(name) {
  tab.value = name
  page.value = 1
  return run(load)
}
</script>

<template>
  <main v-if="!jwt" class="login">
    <h1>管理后台</h1>
    <form @submit.prevent="login">
      <template v-if="setup">
        <p>请用身份验证器导入下方地址，并输入当前验证码。</p>
        <el-input :model-value="setup.otpauthUri" readonly type="textarea" :rows="3" aria-label="绑定地址"/>
      </template>
      <template v-else>
        <el-input v-model="username" placeholder="管理员账号" aria-label="管理员账号" autocomplete="username"/>
        <el-input v-model="password" placeholder="密码" aria-label="密码" type="password" autocomplete="current-password"/>
      </template>
      <el-input v-model="totp" placeholder="身份验证码" aria-label="身份验证码" inputmode="numeric" maxlength="6"/>
      <el-button type="primary" native-type="submit" :loading="busy">{{ setup ? '绑定并登录' : '登录' }}</el-button>
    </form>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  </main>
  <div v-else class="shell">
    <aside><h2>管理后台</h2>
      <el-button v-for="(entry, name) in tabs" :key="name" :type="tab === name ? 'primary' : 'default'" @click="go(name)">{{ entry.title }}</el-button>
    </aside>
    <main>
      <header><div><h1>{{ current.title }}</h1></div><el-button @click="jwt = ''; rows = []; result = ''">退出</el-button></header>
      <div class="bar">
        <el-button v-if="['provider', 'model'].includes(tab)" type="primary" @click="openEdit(tab)">新增</el-button>
        <el-button v-if="tab === 'model'" @click="configure('/model/bindings', '用途绑定')">用途绑定</el-button>
        <el-button v-if="tab === 'model'" @click="configure('/points/rules', '积分规则')">积分规则</el-button>
        <el-button v-if="tab === 'moderation'" @click="configure('/moderation/terms', '敏感词')">敏感词</el-button>
        <el-button v-if="tab === 'model'" @click="run(async () => { result = JSON.stringify(await api('/gateway/sync', 'POST')) })">同步网关</el-button>
        <el-button @click="run(load)">刷新</el-button>
      </div>
      <el-alert v-if="error" :title="error" type="error" :closable="false"/>
      <el-table v-loading="busy" :data="rows" border empty-text="暂无数据">
        <el-table-column v-for="[prop, label] in current.columns" :key="prop" :prop="prop" :label="label"/>
        <el-table-column label="操作" min-width="235">
          <template #default="{ row }">
            <el-button v-if="['provider', 'model'].includes(tab)" link type="primary" @click="openEdit(tab, row)">编辑</el-button>
            <el-button v-if="tab === 'provider'" link @click="action('provider', row)">测试</el-button>
            <template v-if="tab === 'model'"><el-button link @click="action('chat', row)">试聊</el-button><el-button link @click="action('impact', row)">下线影响</el-button></template>
            <template v-if="tab === 'moderation'"><el-button link @click="action('false_positive', row)">误判</el-button><el-button link @click="action('confirmed', row)">确认违规</el-button></template>
            <template v-if="tab === 'user'"><el-button v-if="['active','banned'].includes(row.status)" link @click="action('ban', row)">{{ row.status === 'active' ? '封禁' : '解封' }}</el-button><el-button v-if="row.status === 'active'" link @click="action('grant', row)">赠送积分</el-button></template>
            <el-button v-if="tab === 'audit'" link @click="result = JSON.stringify(row, null, 2)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="paginated" class="bar"><el-button :disabled="page <= 1" @click="page--; run(load)">上一页</el-button><span>第 {{ page }} 页</span><el-button :disabled="rows.length < 20" @click="page++; run(load)">下一页</el-button></div>
      <pre v-if="result">{{ result }}</pre>
    </main>
    <el-dialog :model-value="Boolean(editor)" :title="editor?.title" :close-on-click-modal="false" @close="editor = null">
      <el-input v-if="editor" v-model="editor.text" type="textarea" :rows="14" aria-label="配置 JSON" spellcheck="false"/>
      <p v-if="error" class="error">{{ error }}</p>
      <template #footer><el-button @click="editor = null">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<style>
body { margin:0; color:#172033; background:#f5f7fb; font:14px/1.5 Inter,"Microsoft YaHei",sans-serif; }
.shell { display:grid; grid-template-columns:200px minmax(0,1fr); min-height:100vh; }
aside { padding:24px 16px; background:#172033; color:#fff; display:flex; align-items:stretch; flex-direction:column; gap:10px; }
aside .el-button { margin:0; }
main:not(.login) { padding:26px; }
header,.bar { display:flex; align-items:center; gap:10px; margin:0 0 16px; }
header { justify-content:space-between; }
h1,h2 { margin:0 0 12px; }
.login { width:min(90vw,390px); box-sizing:border-box; margin:12vh auto; padding:28px; background:#fff; border:1px solid #dbe2ed; border-radius:14px; }
.login form { display:grid; gap:13px; margin-bottom:12px; }
.el-alert { margin-bottom:12px; }
pre { padding:16px; border:1px solid #dbe2ed; border-radius:10px; background:#fff; white-space:pre-wrap; overflow-wrap:anywhere; }
.error { color:#b42318; }
@media(max-width:700px) { .shell { grid-template-columns:1fr; } aside { flex-direction:row; flex-wrap:wrap; } }
</style>
