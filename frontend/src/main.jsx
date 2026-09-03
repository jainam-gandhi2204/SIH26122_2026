import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

function App() {
  return (
    <main>
      <p className="eyebrow">SIH 2026 · SIH26122</p>
      <h1>Infrastructure Schedule Intelligence</h1>
      <p>Project foundation is ready. Application features will be added next.</p>
    </main>
  )
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
