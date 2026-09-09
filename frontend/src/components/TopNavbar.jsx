import React, { useEffect, useRef } from 'react';

/**
 * Top Navbar component with search and user controls.
 */
export default function TopNavbar({ searchQuery = '', onSearchChange, notificationCount = 0 }) {
  const searchInputRef = useRef(null);

  // Keyboard shortcut: Ctrl+K or Cmd+K to focus search input
  useEffect(() => {
    const handleKeyDown = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        searchInputRef.current?.focus();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  return (
    <header className="h-16 px-8 bg-transparent flex items-center justify-between shrink-0">
      {/* Search Activity Input */}
      <div className="relative w-[400px]">
        <div className="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-slate-400">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
          </svg>
        </div>
        <input
          ref={searchInputRef}
          value={searchQuery}
          onChange={(e) => onSearchChange && onSearchChange(e.target.value)}
          className="w-full bg-white border border-slate-200 rounded-full pl-10 pr-16 py-1.5 text-[12.5px] text-slate-700 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-cyan-500/20 focus:border-cyan-500 transition shadow-sm"
          placeholder="Search activity ID, CPM path, or update..."
          type="text"
        />
        <span className="absolute inset-y-0 right-0 pr-3 flex items-center pointer-events-none">
          <kbd className="text-[10px] font-semibold text-slate-400 bg-slate-100 border border-slate-200 rounded px-1.5 py-0.5">
            Ctrl K
          </kbd>
        </span>
      </div>

      {/* User & Notifications Controls */}
      <div className="flex items-center gap-4">
        {/* Notification Bell with Ping Dot */}
        <button
          aria-label="Notifications"
          className="relative p-2 text-slate-500 hover:text-slate-700 transition cursor-pointer"
          title={`${notificationCount} alert${notificationCount === 1 ? '' : 's'}`}
        >
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path
              d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="2"
            />
          </svg>
          {notificationCount > 0 && (
            <span className="absolute top-1.5 right-1.5 w-2.5 h-2.5 bg-red-500 border-2 border-white rounded-full" />
          )}
        </button>

        {/* User Profile Pill */}
        <div className="flex items-center gap-3 pl-2 select-none group">
          <div className="w-9 h-9 rounded-full bg-[#0284c7] text-white flex items-center justify-center font-bold text-sm shadow-sm">
            J
          </div>
          <div className="text-left">
            <div className="text-[13px] font-bold text-slate-800 leading-tight">Chief Project Planner</div>
            <div className="text-[11px] text-slate-500 font-medium">Senior PM Office</div>
          </div>
          <svg className="w-4 h-4 text-slate-400 group-hover:text-slate-600 transition ml-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path d="M19 9l-7 7-7-7" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
          </svg>
        </div>
      </div>
    </header>
  );
}

