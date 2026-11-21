import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import ConfirmDialog from '../src/components/ConfirmDialog.vue'

const BASE = {
  open: true,
  title: 'Unsaved changes',
  message: 'Pick a path.',
  confirmLabel: 'Save and continue',
}

describe('confirm dialog', () => {
  it('renders the alternate action only when configured', async () => {
    const wrapper = mount(ConfirmDialog, { props: { ...BASE } })
    expect(wrapper.find('[data-testid="alternate"]').exists()).toBe(false)

    await wrapper.setProps({ alternateLabel: 'Continue without saving' })
    expect(wrapper.get('[data-testid="alternate"]').text()).toBe(
      'Continue without saving',
    )
  })

  it('emits alternate from the third button', async () => {
    const wrapper = mount(ConfirmDialog, {
      props: { ...BASE, alternateLabel: 'Continue without saving' },
    })

    await wrapper.get('[data-testid="alternate"]').trigger('click')

    expect(wrapper.emitted('alternate')).toHaveLength(1)
    expect(wrapper.emitted('confirm')).toBeUndefined()
    expect(wrapper.emitted('cancel')).toBeUndefined()
  })
})
