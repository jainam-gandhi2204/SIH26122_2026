import React, { useMemo } from 'react';
import { formatDateShort } from '../utils/formatters';

/**
 * Dynamic Gantt Timeline component mapping schedule tasks to calendar days.
 * Dynamically scales to real backend planned and actual dates.
 */
export default function GanttTimeline({ tasks = [], isLoading = false, onSelectTask }) {
  // Compute timeline boundaries and header ticks dynamically
  const { timelineStart, totalDurationMs, headerPeriod, dateTicks, todayLeftPct } = useMemo(() => {
    let minTime = Infinity;
    let maxTime = -Infinity;

    tasks.forEach((t) => {
      const dates = [t.planned_start, t.planned_end, t.actual_start, t.actual_end];
      dates.forEach((d) => {
        if (!d) return;
        const dt = new Date(d);
        const time = dt.getTime();
        if (!isNaN(time)) {
          if (time < minTime) minTime = time;
          if (time > maxTime) maxTime = time;
        }
      });
    });

    // Fallback if no valid dates found (e.g. September 2026 default)
    if (minTime === Infinity || maxTime === -Infinity) {
      const start = new Date(2026, 8, 1);
      const end = new Date(2026, 8, 30);
      return {
        timelineStart: start,
        totalDurationMs: end.getTime() - start.getTime() + 86400000,
        headerPeriod: 'September 2026',
        dateTicks: [
          new Date(2026, 8, 1),
          new Date(2026, 8, 8),
          new Date(2026, 8, 15),
          new Date(2026, 8, 22),
          new Date(2026, 8, 29),
        ],
        todayLeftPct: 28, // Near Sep 09
      };
    }

    const startDate = new Date(minTime);
    startDate.setHours(0, 0, 0, 0);

    const endDate = new Date(maxTime);
    endDate.setHours(23, 59, 59, 999);

    let durationMs = endDate.getTime() - startDate.getTime();
    if (durationMs < 7 * 86400000) {
      // Expand to at least 7 days for readability
      durationMs = 7 * 86400000;
    }

    // Header label
    const sMonth = startDate.toLocaleDateString('en-US', { month: 'short' });
    const eMonth = endDate.toLocaleDateString('en-US', { month: 'short' });
    const sYear = startDate.getFullYear();
    const eYear = endDate.getFullYear();
    let periodStr = `${startDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })}`;
    if (sMonth !== eMonth || sYear !== eYear) {
      periodStr = `${sMonth} ${sYear} – ${eMonth} ${eYear}`;
    }

    // 5 header date ticks across duration
    const ticks = [
      new Date(startDate.getTime()),
      new Date(startDate.getTime() + durationMs * 0.25),
      new Date(startDate.getTime() + durationMs * 0.50),
      new Date(startDate.getTime() + durationMs * 0.75),
      new Date(startDate.getTime() + durationMs),
    ];

    // Reference today marker (check 09-Sep-2026 prototype date or today's date)
    let todayTime = new Date('2026-09-09').getTime();
    const actualNow = Date.now();
    if (actualNow >= startDate.getTime() && actualNow <= startDate.getTime() + durationMs) {
      todayTime = actualNow;
    }

    let todayPct = null;
    if (todayTime >= startDate.getTime() && todayTime <= startDate.getTime() + durationMs) {
      todayPct = Math.max(2, Math.min(98, ((todayTime - startDate.getTime()) / durationMs) * 100));
    }

    return {
      timelineStart: startDate,
      totalDurationMs: durationMs,
      headerPeriod: periodStr,
      dateTicks: ticks,
      todayLeftPct: todayPct,
    };
  }, [tasks]);

  const calculateBarPosition = (task) => {
    let startMs = timelineStart.getTime();
    let endMs = startMs + 86400000;

    if (task.planned_start) {
      const d = new Date(task.planned_start);
      if (!isNaN(d.getTime())) startMs = d.getTime();
    }
    if (task.planned_end) {
      const d = new Date(task.planned_end);
      if (!isNaN(d.getTime())) endMs = d.getTime() + 86400000;
    }

    if (endMs < startMs) endMs = startMs + 86400000;

    const leftPct = Math.max(0, Math.min(95, ((startMs - timelineStart.getTime()) / totalDurationMs) * 100));
    const durationMs = Math.max(86400000, endMs - startMs);
    const widthPct = Math.max(3, Math.min(100 - leftPct, (durationMs / totalDurationMs) * 100));

    const progress = Math.min(
      100,
      Math.max(0, Math.round(task.progress_percent ?? task.actual_progress ?? 0))
    );
    const actualWidthPct = (progress / 100) * widthPct;

    const s = (task.status || '').toLowerCase();
    let actualBarColor = 'bg-teal-500';
    let plannedBarColor = 'bg-slate-200/90';

    if (s.includes('delay')) {
      actualBarColor = 'bg-rose-500';
      plannedBarColor = 'bg-red-100 border border-red-200';
    } else if (s.includes('risk')) {
      actualBarColor = 'bg-amber-500';
      plannedBarColor = 'bg-amber-100/70 border border-amber-200/50';
    } else if (s.includes('progress') || s.includes('on_time')) {
      actualBarColor = 'bg-blue-600';
      plannedBarColor = 'bg-blue-50 border border-blue-100';
    } else if (s.includes('complete')) {
      actualBarColor = 'bg-teal-500';
      plannedBarColor = 'bg-teal-100/60';
    }

    return {
      left: `${leftPct}%`,
      plannedWidth: `${widthPct}%`,
      actualWidth: `${actualWidthPct}%`,
      actualBarColor,
      plannedBarColor,
      progress,
    };
  };

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] p-4 flex flex-col justify-between h-full">
      <div>
        {/* Header with Period Badge */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <span className="p-1.5 bg-sky-50 text-sky-600 rounded-lg">
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                <path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z" />
              </svg>
            </span>
            <h2 className="font-extrabold text-[14px] text-slate-800">
              Schedule Timeline (Gantt View)
            </h2>
          </div>
          <div className="flex items-center gap-1 border border-slate-200 rounded-lg px-2.5 py-1 text-[11px] font-semibold text-slate-700 bg-white">
            <span>{headerPeriod}</span>
            <svg className="w-3.5 h-3.5 text-slate-400 ml-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path d="M19 9l-7 7-7-7" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
            </svg>
          </div>
        </div>

        {/* Gantt Chart Container */}
        <div className="relative pt-2">
          {/* Gantt Header (Dates) */}
          <div className="grid grid-cols-5 text-center text-[10px] font-semibold text-slate-400 pb-2 border-b border-slate-100 pl-10">
            {dateTicks.map((tick, idx) => (
              <div key={`tick-${idx}`}>{formatDateShort(tick)}</div>
            ))}
          </div>

          {/* Vertical Timeline Grid & 'Today' Marker */}
          <div className="absolute inset-0 top-6 left-10 right-0 pointer-events-none grid grid-cols-5 border-l border-slate-100">
            <div className="border-r border-slate-100" />
            <div className="border-r border-slate-100" />
            <div className="border-r border-slate-100" />
            <div className="border-r border-slate-100" />
            <div />

            {/* 'Today' vertical marker */}
            {todayLeftPct !== null && (
              <div
                className="absolute top-0 bottom-0 border-r-2 border-dashed border-sky-600 z-10"
                style={{ left: `${todayLeftPct}%` }}
              />
            )}
          </div>

          {/* Gantt Rows */}
          {isLoading ? (
            <div className="space-y-4 pt-3 pl-2 relative z-0">
              {Array.from({ length: 6 }).map((_, idx) => (
                <div key={`gantt-skel-${idx}`} className="flex items-center animate-pulse">
                  <div className="w-10 h-3 bg-slate-200 rounded mr-2" />
                  <div className="flex-1 relative h-4">
                    <div
                      className="h-3 bg-slate-200 rounded"
                      style={{
                        marginLeft: `${idx * 14}%`,
                        width: `${Math.max(15, 45 - idx * 5)}%`,
                      }}
                    />
                  </div>
                </div>
              ))}
            </div>
          ) : tasks.length === 0 ? (
            <div className="py-12 text-center text-slate-400 text-[12px] relative z-10">
              No timeline activities available.
            </div>
          ) : (
            <div className="space-y-3.5 pt-3 text-[11px] font-bold text-slate-600 relative z-0">
              {tasks.map((task) => {
                const pos = calculateBarPosition(task);

                return (
                  <div
                    key={task.task_id}
                    onClick={() => onSelectTask && onSelectTask(task.task_id)}
                    className="flex items-center cursor-pointer group"
                    title={`${task.task_id}: ${task.task_name} (${pos.progress}% complete)`}
                  >
                    <span className="w-10 text-slate-500 font-semibold group-hover:text-cyan-700 transition">
                      {task.task_id}
                    </span>
                    <div className="flex-1 relative h-4">
                      {/* Planned baseline bar */}
                      <div
                        className={`absolute h-3 rounded transition-all ${pos.plannedBarColor}`}
                        style={{ left: pos.left, width: pos.plannedWidth }}
                      />
                      {/* Actual progress bar */}
                      {pos.progress > 0 && (
                        <div
                          className={`absolute h-3 rounded transition-all shadow-xs ${pos.actualBarColor}`}
                          style={{ left: pos.left, width: pos.actualWidth }}
                        />
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Gantt Footer Legend */}
      <div className="flex items-center justify-end gap-5 pt-4 mt-3 border-t border-slate-100 text-[11px] text-slate-500">
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 bg-slate-200 rounded-xs inline-block" />
          <span>Planned</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 bg-teal-500 rounded-xs inline-block" />
          <span>Actual Progress</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-3 border-t-2 border-dashed border-sky-600 inline-block" />
          <span>Today</span>
        </div>
      </div>
    </div>
  );
}


