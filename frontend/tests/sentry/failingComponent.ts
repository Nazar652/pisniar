import { defineComponent } from 'vue'

export const FailingComponent = defineComponent({
  props: { initData: { type: String, required: true } },
  setup() {
    throw new Error('setup failed')
  },
})
