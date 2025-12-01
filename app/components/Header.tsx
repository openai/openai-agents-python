import Link from 'next/link'

export default function Header() {
  return (
    <header className="border-b bg-white">
      <div className="max-w-8xl mx-auto px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-gradient-to-br from-indigo-500 to-purple-600 rounded-md flex items-center justify-center text-white font-semibold">AG</div>
          <div className="text-lg font-semibold">AdGen AI Platform</div>
        </div>

        <nav className="text-sm text-slate-600 flex items-center gap-6">
          <Link href="/">Home</Link>
          <Link href="/campaigns/create" className="text-indigo-600 font-semibold">Create</Link>
          <Link href="/campaigns">Campaigns</Link>
        </nav>
      </div>
    </header>
  )
}
