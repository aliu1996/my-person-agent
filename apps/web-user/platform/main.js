import { createPinia } from 'pinia'
import { createApp } from 'vue'
import Platform from './Platform.vue'
import './styles.css'

createApp(Platform).use(createPinia()).mount('#app')
