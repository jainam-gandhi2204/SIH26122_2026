import React from 'react';
import { formatDateTime } from '../utils/formatters';

/**
 * Header row with project title, location, timestamp, and primary action buttons.
 */
export default function HeaderRow({
  lastUpdated,
  isRefreshing = false,
  onRefresh,
  onOpenNewUpdate,
  onOpenUpload,
}) {
  return (
    <div className="flex items-end justify-between pt-1 pb-1">
      <div>
        <div className="text-[12px] font-semibold text-slate-500 mb-1 tracking-wide">
          <span className="text-slate-700 font-bold">SIH26122</span>{' '}
          <span className="mx-1 text-slate-300">|</span> Intelligent Data Capture &amp; Schedule-Linking Layer
        </div>
        <h1 className="text-[26px] font-extrabold text-[#091628] tracking-tight leading-tight">
          Executive Project Overview
        </h1>
        <p className="text-[13px] text-slate-500 mt-0.5">
          Real-time site updates. Accurate progress. Better decisions.
        </p>
      </div>

      {/* Meta Information Cards & Actions on Right */}
      <div className="flex items-center gap-3">
        {/* Well Pad Card */}
        <div className="bg-white/80 backdrop-blur-sm border border-slate-200/80 rounded-xl px-4 py-2.5 flex items-center gap-3 shadow-xs">
          <div className="text-sky-600">
            <svg className="w-6 h-6 fill-sky-800 text-sky-800" viewBox="0 0 24 24">
              <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5s1.12-2.5 2.5-2.5 2.5 1.12 2.5 2.5-1.12 2.5-2.5 2.5z" />
            </svg>
          </div>
          <div>
            <div className="text-[13px] font-bold text-slate-800 leading-tight">Well Pad A</div>
            <div className="text-[11px] text-slate-500">Oil India Limited</div>
          </div>
        </div>

        {/* Timestamp Card */}
        <div className="bg-white/80 backdrop-blur-sm border border-slate-200/80 rounded-xl px-4 py-2.5 flex items-center gap-3 shadow-xs">
          <div className="text-sky-700">
            <svg className="w-5 h-5 text-sky-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
            </svg>
          </div>
          <div>
            <div className="text-[11px] font-semibold text-slate-600 leading-tight">Last Synced</div>
            <div className="text-[13px] font-bold text-slate-800">
              {lastUpdated ? formatDateTime(lastUpdated) : 'Live'}
            </div>
          </div>
        </div>

        {/* Action: New Site Update */}
        <button
          onClick={onOpenNewUpdate}
          className="px-3.5 py-2.5 bg-teal-700 hover:bg-teal-800 text-white rounded-xl font-bold text-[12px] flex items-center gap-1.5 shadow-sm transition cursor-pointer"
          title="Record a field progress update"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path d="M12 4v16m8-8H4" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" />
          </svg>
          <span>New Update</span>
        </button>

        {/* Action: Upload File */}
        <button
          onClick={onOpenUpload}
          className="px-3.5 py-2.5 bg-white hover:bg-slate-50 border border-slate-200 text-slate-700 rounded-xl font-semibold text-[12px] flex items-center gap-1.5 shadow-xs transition cursor-pointer"
          title="Upload CSV / XLSX spreadsheet"
        >
          <svg className="w-4 h-4 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
          </svg>
          <span>Upload File</span>
        </button>

        {/* Action: Refresh Data */}
        <button
          onClick={onRefresh}
          disabled={isRefreshing}
          className="p-2.5 bg-white hover:bg-slate-50 border border-slate-200 text-slate-600 rounded-xl font-semibold text-[12px] flex items-center justify-center shadow-xs transition cursor-pointer disabled:opacity-50"
          title="Refresh dashboard data"
          aria-label="Refresh dashboard"
        >
          <svg
            className={`w-4 h-4 text-slate-500 ${isRefreshing ? 'animate-spin' : ''}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
          </svg>
        </button>
      </div>
    </div>
  );
}

