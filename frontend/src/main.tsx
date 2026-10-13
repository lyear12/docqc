/**
 * Browser entry point.
 *
 * `StrictMode` is on deliberately even though it double-invokes effects in
 * development. The app holds a live `EventSource` and a server-side job, so
 * an effect that is not safe to run twice is a real bug — and StrictMode is
 * what surfaces it. The stream cleanup in `App` is written for exactly that.
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'

const container = document.getElementById('root')
if (container === null) {
  throw new Error('#root is missing from index.html')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
