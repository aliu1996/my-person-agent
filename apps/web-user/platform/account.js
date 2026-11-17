import { defineStore } from 'pinia'
import { api, ApiError } from './api.mjs'

export const useAccount = defineStore('account', {
  state: () => ({ jwt: '', refreshToken: '', profile: null }),
  actions: {
    async login(phone, otpCode) {
      const credentials = await api('/auth/loginByPhone', { method: 'POST', body: { phone, otpCode } })
      this.storeCredentials(credentials)
      this.profile = await this.request('/user/profile')
    },
    storeCredentials(credentials) {
      this.jwt = credentials.userJwt
      this.refreshToken = credentials.refreshToken
    },
    async request(path, options = {}) {
      try {
        return await api(path, { ...options, jwt: this.jwt })
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 401 || !this.refreshToken) throw error
        const credentials = await api('/auth/refresh', {
          method: 'POST', body: { refreshToken: this.refreshToken },
        })
        this.storeCredentials(credentials)
        return api(path, { ...options, jwt: this.jwt })
      }
    },
    async logout() {
      this.$reset()
    },
  },
})
