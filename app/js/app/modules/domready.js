import _ from 'lodash'

const EVENT_DOM_READY = 'DOMContentLoaded'

let callbacks = []

const onReady = () => {
  _.forEach(callbacks, (fn) => {
    fn.call()
  })
  document.removeEventListener(EVENT_DOM_READY, onReady)
}

export default function ready(fn) {
  callbacks.push(fn)
  if (document.readyState !== 'loading') {
    onReady.call()
  } else {
    document.addEventListener(EVENT_DOM_READY, onReady)
  }
}
