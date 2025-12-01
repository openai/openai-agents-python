import React from 'react'

export default function Home() {
  return (
    <div className="max-w-6xl mx-auto">
      <div className="bg-white shadow-md rounded-md px-8 py-10">
        <h1 className="text-2xl font-bold">AdGen AI Platform</h1>
        <p className="mt-2 text-slate-600">Internal-only scaffold for building AI-powered OOH/DOOH/Social/Print ad campaigns.
        This repository area contains the UI pages, components, lib, and API route placeholders for future integrations.</p>

        <div className="mt-6 grid grid-cols-1 sm:grid-cols-2 gap-4">
          <a className="p-4 border rounded hover:shadow-md" href="/campaigns/create">
            <h3 className="font-semibold">Create a campaign</h3>
            <p className="text-sm text-slate-500 mt-1">Step through the campaign creation workflow.</p>
          </a>

          <a className="p-4 border rounded hover:shadow-md" href="/campaigns">
            <h3 className="font-semibold">View campaigns</h3>
            <p className="text-sm text-slate-500 mt-1">Manage and explore existing campaigns (UI placeholder).</p>
          </a>
        </div>
      </div>
    </div>
  )
}
