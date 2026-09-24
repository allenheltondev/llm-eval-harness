import '@testing-library/jest-dom'

// jsdom has no <dialog> modality. The design system's Modal (the AppNav
// profile menu and app launcher) calls showModal()/close(); give it the
// minimal behavior -- toggling `open` -- so those menus render in tests.
if (typeof HTMLDialogElement !== 'undefined' && !HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true
  }
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false
    this.dispatchEvent(new Event('close'))
  }
}
