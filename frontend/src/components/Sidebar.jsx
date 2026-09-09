import React from 'react';

/**
 * Sidebar navigation component replicating the ConstructLink Stitch layout.
 */
export default function Sidebar({ activeTab = 'overview', onSelectTab, pendingReviewCount = 0 }) {
  const navItems = [
    {
      id: 'overview',
      label: 'Executive Overview',
      icon: (
        <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
        </svg>
      ),
    },
    {
      id: 'schedule',
      label: 'Master Schedule',
      icon: (
        <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
        </svg>
      ),
    },
    {
      id: 'ingestion',
      label: 'Site Ingestion & Extraction',
      icon: (
        <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
        </svg>
      ),
    },
    {
      id: 'review',
      label: 'Planner Review Queue',
      badge: pendingReviewCount,
      icon: (
        <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
        </svg>
      ),
    },
    {
      id: 'risk',
      label: 'Downstream Risk Analysis',
      icon: (
        <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
        </svg>
      ),
    },
    {
      id: 'settings',
      label: 'Settings & Audit Log',
      icon: (
        <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
          <path d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
        </svg>
      ),
    },
  ];

  return (
    <aside className="w-64 bg-[#091628] text-slate-300 flex flex-col justify-between shrink-0 fixed inset-y-0 left-0 z-30 select-none border-r border-[#15243b]">
      <div>
        {/* Brand Logo & Header */}
        <div className="px-5 py-5 flex items-center gap-3 border-b border-[#14233a]">
          <div className="w-9 h-9 rounded-lg bg-cyan-500/10 border border-cyan-400/30 flex items-center justify-center shrink-0">
            <svg className="w-6 h-6 text-cyan-400" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" viewBox="0 0 24 24">
              <polygon points="12 2 2 7 12 12 22 7 12 2" />
              <polyline points="2 17 12 22 22 17" />
              <polyline points="2 12 12 17 22 12" />
            </svg>
          </div>
          <div>
            <div className="text-[14px] font-extrabold tracking-wide text-white leading-tight">CONSTRUCTLINK</div>
            <div className="text-[11px] text-slate-400 font-medium">Oil India Limited</div>
          </div>
        </div>

        {/* Navigation Menu */}
        <nav className="p-3 space-y-1 mt-2 font-medium text-[13px]">
          {navItems.map((item) => {
            const isActive = activeTab === item.id;
            return (
              <button
                key={item.id}
                onClick={() => onSelectTab && onSelectTab(item.id)}
                className={`w-full flex items-center justify-between px-3.5 py-2.5 rounded-lg transition text-left cursor-pointer ${
                  isActive
                    ? 'bg-[#0e3a59] text-cyan-300 shadow-sm border border-cyan-500/20 font-semibold'
                    : 'text-slate-300 hover:text-white hover:bg-slate-800/60'
                }`}
              >
                <div className="flex items-center gap-3">
                  <span className={isActive ? 'text-cyan-400' : 'text-slate-400'}>
                    {item.icon}
                  </span>
                  <span>{item.label}</span>
                </div>
                {item.badge !== undefined && item.badge > 0 && (
                  <span className="w-5 h-5 flex items-center justify-center rounded-full bg-orange-500 text-white font-bold text-[10px]">
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>
      </div>

      {/* Bottom Illustration and Slogan */}
      <div className="px-5 py-6 relative overflow-hidden bg-gradient-to-b from-[#091628] to-[#060e1a]">
        <div className="opacity-30 mb-3.5 pointer-events-none flex justify-center">
          <svg className="w-full h-24 text-cyan-300" fill="none" stroke="currentColor" strokeWidth="1.4" viewBox="0 0 200 110">
            <path d="M45 105 L60 20 L75 105 Z" />
            <path d="M50 85 L70 85" />
            <path d="M53 65 L67 65" />
            <path d="M56 45 L64 45" />
            <path d="M50 85 L67 65" />
            <path d="M70 85 L53 65" />
            <path d="M53 65 L64 45" />
            <path d="M67 65 L56 45" />
            <line strokeWidth="1.5" x1="60" x2="60" y1="20" y2="10" />
            <rect height="50" rx="3" width="22" x="90" y="55" />
            <line x1="90" x2="112" y1="65" y2="65" />
            <line x1="90" x2="112" y1="75" y2="75" />
            <line x1="90" x2="112" y1="85" y2="85" />
            <line x1="90" x2="112" y1="95" y2="95" />
            <line x1="101" x2="101" y1="55" y2="35" />
            <line x1="101" x2="120" y1="35" y2="35" />
            <line x1="120" x2="120" y1="35" y2="65" />
            <rect height="60" rx="2" width="16" x="125" y="45" />
            <path d="M148 105 C148 85 180 85 180 105" />
            <line strokeWidth="1.5" x1="0" x2="200" y1="105" y2="105" />
          </svg>
        </div>
        <div>
          <h4 className="text-white font-bold text-[13px] leading-tight mb-2">
            Infrastructure<br />Progress, Smarter.
          </h4>
          <p className="text-[11px] text-slate-400 leading-relaxed">
            Field Updates.<br />Accurate Schedules.<br />Better Decisions.
          </p>
        </div>
      </div>
    </aside>
  );
}

