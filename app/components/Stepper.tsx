import React from 'react'

export default function Stepper({ steps, active = 0 }: { steps: string[]; active?: number }) {
  return (
    <div className="flex items-center gap-3">
      {steps.map((s, i) => (
        <div key={s} className="flex items-center gap-3">
          <div className={`w-8 h-8 rounded-full flex items-center justify-center ${i <= active ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-400'}`}>{i + 1}</div>
          <div className={`text-sm ${i <= active ? 'text-slate-800' : 'text-slate-400'}`}>{s}</div>
        </div>
      ))}
    </div>
  )
}
