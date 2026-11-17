import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import Admin from './Admin.vue'

createApp(Admin).use(ElementPlus).mount('#app')
