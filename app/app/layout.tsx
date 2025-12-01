import './globals.css'
import React from 'react'

export const metadata = {
  title: 'AdGen AI Platform — Internal',
  description: 'Internal scaffold for AdGen AI Platform — create and manage ad campaigns',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="min-h-screen flex flex-col">
          <header className="border-b bg-white">
            <div className="max-w-8xl mx-auto px-6 py-4 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 bg-gradient-to-br from-indigo-500 to-purple-600 rounded-md flex items-center justify-center text-white font-semibold">AG</div>
                <div className="text-lg font-semibold">AdGen AI Platform</div>
              </div>
              <nav className="text-sm text-slate-600 flex items-center gap-6">
                <a href="/" className="hover:underline">Home</a>
                <a href="/campaigns/create" className="text-indigo-600 font-semibold">Create Campaign</a>
              </nav>
            </div>
          </header>

          <main className="py-8 px-6 max-w-8xl mx-auto w-full">{children}</main>

          <footer className="mt-auto border-t bg-white">
            <div className="max-w-8xl mx-auto px-6 py-4 text-xs text-slate-500">Internal-only AdGen AI Platform • For development and testing only</div>
          </footer>
        </div>
      </body>
    </html>
  )
}
