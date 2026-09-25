import React, { useState } from 'react';
import { ingestSiteUpdate, uploadSiteUpdatesSpreadsheet, importScheduleSpreadsheet } from '../api/client';

/**
 * Modal dialog for free-text site update ingestion and CSV/XLSX spreadsheet upload.
 */
export default function IngestModal({
  isOpen,
  onClose,
  initialTab = 'text',
  initialUploadMode = 'updates',
  onSuccess,
}) {
  const [tab, setTab] = useState(initialTab);
  const [text, setText] = useState('');
  const [location, setLocation] = useState('');
  const [reportedDate, setReportedDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [selectedFile, setSelectedFile] = useState(null);
  const [uploadMode, setUploadMode] = useState(initialUploadMode); // 'updates' or 'schedule'
  const [replaceSchedule, setReplaceSchedule] = useState(false);
  const [confirmReplace, setConfirmReplace] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [feedback, setFeedback] = useState(null); // { type: 'success' | 'error', message: string, details?: any }

  // Sync tab & uploadMode with props if changed
  React.useEffect(() => {
    setTab(initialTab);
    setUploadMode(initialUploadMode);
    setReplaceSchedule(false);
    setConfirmReplace(false);
    setSelectedFile(null);
    setFeedback(null);
    setLocation('');
  }, [initialTab, initialUploadMode, isOpen]);

  if (!isOpen) return null;

  const handlePreFill = (sampleText) => {
    setText(sampleText);
    setFeedback(null);
  };

  const handleTextSubmit = async (e) => {
    e.preventDefault();
    if (!text.trim()) return;

    setIsLoading(true);
    setFeedback(null);
    try {
      const res = await ingestSiteUpdate({
        text: text.trim(),
        reported_date: reportedDate || undefined,
        location: location.trim() || undefined,
      });

      setFeedback({
        type: 'success',
        message: 'Site update processed and extracted successfully by AI!',
        details: res,
      });
      setText('');
      setLocation('');
      onSuccess?.();
    } catch (err) {
      setFeedback({
        type: 'error',
        message: err.message || 'Failed to ingest site update.',
      });
    } finally {
      setIsLoading(false);
    }
  };

  const handleFileSubmit = async (e) => {
    e.preventDefault();
    if (!selectedFile) return;

    if (uploadMode === 'schedule' && replaceSchedule && !confirmReplace) {
      setFeedback({
        type: 'error',
        message: 'Please confirm that you want to replace the current project schedule.',
      });
      return;
    }

    setIsLoading(true);
    setFeedback(null);
    try {
      let res;
      if (uploadMode === 'schedule') {
        res = await importScheduleSpreadsheet(selectedFile, replaceSchedule);
      } else {
        res = await uploadSiteUpdatesSpreadsheet(selectedFile);
      }

      const successMsg =
        uploadMode === 'schedule'
          ? replaceSchedule
            ? 'Project schedule baseline replaced and imported successfully!'
            : 'Project schedule imported and updated successfully!'
          : res.message || 'Spreadsheet uploaded and processed successfully!';

      setFeedback({
        type: 'success',
        message: successMsg,
        details: res,
      });
      setSelectedFile(null);
      setReplaceSchedule(false);
      setConfirmReplace(false);
      onSuccess?.();
    } catch (err) {
      setFeedback({
        type: 'error',
        message: err.message || 'Spreadsheet processing failed.',
      });
    } finally {
      setIsLoading(false);
    }
  };

  const primaryObs = Array.isArray(feedback?.details)
    ? (feedback.details[0] || null)
    : feedback?.details;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-xs transition-opacity animate-in fade-in">
      <div className="bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-xl overflow-hidden flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between bg-[#091628] text-white">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-cyan-500/20 border border-cyan-500/40 flex items-center justify-center text-cyan-400">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
              </svg>
            </div>
            <div>
              <h3 className="font-extrabold text-[15px] leading-tight text-white">
                Site Ingestion &amp; Schedule-Linking
              </h3>
              <p className="text-[11px] text-slate-300">
                Submit raw field notes or upload structured operational spreadsheets
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 flex items-center justify-center transition cursor-pointer"
          >
            ✕
          </button>
        </div>

        {/* Tab Switcher */}
        <div className="flex border-b border-slate-200 bg-slate-50 px-6 pt-3 gap-2">
          <button
            onClick={() => {
              setTab('text');
              setFeedback(null);
            }}
            className={`pb-2.5 px-3 font-bold text-[12.5px] border-b-2 transition cursor-pointer ${
              tab === 'text'
                ? 'border-teal-600 text-teal-700'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            Field Note (Free Text)
          </button>
          <button
            onClick={() => {
              setTab('file');
              setFeedback(null);
            }}
            className={`pb-2.5 px-3 font-bold text-[12.5px] border-b-2 transition cursor-pointer ${
              tab === 'file'
                ? 'border-teal-600 text-teal-700'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            Spreadsheet Upload (CSV / XLSX)
          </button>
        </div>

        {/* Modal Body */}
        <div className="p-6 overflow-y-auto space-y-4">
          {/* Feedback Alert */}
          {feedback && (
            <div
              className={`p-3.5 rounded-xl border text-[12px] flex items-start gap-2.5 ${
                feedback.type === 'success'
                  ? 'bg-emerald-50/90 border-emerald-200 text-emerald-900'
                  : 'bg-rose-50/90 border-rose-200 text-rose-900'
              }`}
            >
              <span className="font-bold text-sm">
                {feedback.type === 'success' ? '✓' : '✕'}
              </span>
              <div className="flex-1 space-y-1.5">
                <div className="font-bold">{feedback.message}</div>

                {/* Free-text AI extraction details */}
                {primaryObs && (primaryObs.matched_task_id || primaryObs.model_response || primaryObs.progress_percent !== undefined) && (
                  <div className="text-[11px] bg-white/90 border border-emerald-200 rounded-lg p-2.5 space-y-1 text-slate-800 shadow-xs">
                    <div className="flex items-center justify-between">
                      <span className="text-slate-500 font-semibold">Matched Activity:</span>
                      <span className="font-mono font-bold text-teal-800 bg-teal-50 px-1.5 py-0.5 rounded">
                        {primaryObs.model_response?.matched_source_task_id || (primaryObs.matched_task_id ? 'Linked to Schedule' : 'Unmatched (Sent to Review Queue)')}
                      </span>
                    </div>
                    {primaryObs.progress_percent !== null && primaryObs.progress_percent !== undefined && (
                      <div className="flex items-center justify-between">
                        <span className="text-slate-500 font-semibold">Reported Progress:</span>
                        <span className="font-bold text-slate-800">{primaryObs.progress_percent}%</span>
                      </div>
                    )}
                    {primaryObs.confidence_score !== null && primaryObs.confidence_score !== undefined && (
                      <div className="flex items-center justify-between">
                        <span className="text-slate-500 font-semibold">AI Confidence:</span>
                        <span className={`font-bold ${primaryObs.confidence_score >= 70 ? 'text-teal-700' : 'text-amber-700'}`}>
                          {Math.round(primaryObs.confidence_score)}%
                          {primaryObs.confidence_score < 70 ? ' (Low — Review Required)' : ''}
                        </span>
                      </div>
                    )}
                    {primaryObs.delay_days ? (
                      <div className="flex items-center justify-between text-rose-700 font-semibold">
                        <span>Reported Delay:</span>
                        <span>+{primaryObs.delay_days} day slip ({primaryObs.delay_reason || 'Site condition'})</span>
                      </div>
                    ) : null}
                    {primaryObs.model_response?.reasoning && (
                      <div className="pt-1 text-[10px] text-slate-500 border-t border-slate-100 italic">
                        "{primaryObs.model_response.reasoning}"
                      </div>
                    )}
                  </div>
                )}

                {/* Schedule import details */}
                {feedback.details && feedback.details.tasks_imported !== undefined && (
                  <div className="text-[11px] bg-white/90 border border-emerald-200 rounded-lg p-2.5 space-y-1 text-slate-800 shadow-xs">
                    <div>
                      File: <strong>{feedback.details.filename}</strong>
                    </div>
                    <div className="flex items-center gap-3">
                      <div>
                        Tasks Imported: <strong className="text-teal-700">{feedback.details.tasks_imported}</strong>
                      </div>
                      <div>
                        Dependencies: <strong className="text-teal-700">{feedback.details.dependencies_imported}</strong>
                      </div>
                    </div>
                    <div className="text-[10.5px] text-slate-500 pt-0.5 border-t border-slate-100">
                      Import ID: <span className="font-mono">{feedback.details.import_id}</span>
                    </div>
                  </div>
                )}

                {/* Spreadsheet batch upload details */}
                {feedback.details && feedback.details.stored !== undefined && (
                  <div className="text-[11px] bg-white/90 border border-emerald-200 rounded-lg p-2.5 space-y-1 text-slate-800 shadow-xs">
                    <div>
                      File: <strong>{feedback.details.filename}</strong>
                    </div>
                    <div>
                      Rows Stored: <strong>{feedback.details.stored} of {feedback.details.accepted}</strong>
                      {feedback.details.processed && ` (${feedback.details.processed.length} processed by AI)`}
                    </div>
                    {feedback.details.row_errors && feedback.details.row_errors.length > 0 && (
                      <div className="text-amber-700 text-[10.5px] mt-0.5">
                        ⚠️ {feedback.details.row_errors.length} row(s) had warnings or were skipped.
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}

          {tab === 'text' ? (
            /* Free Text Ingestion Form */
            <form onSubmit={handleTextSubmit} className="space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-[12px] font-bold text-slate-700 mb-1">
                    Reported Date
                  </label>
                  <input
                    type="date"
                    value={reportedDate}
                    onChange={(e) => setReportedDate(e.target.value)}
                    className="w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-[12.5px] text-slate-800 focus:outline-none focus:ring-2 focus:ring-teal-500/20 focus:border-teal-600"
                  />
                </div>
                <div>
                  <label className="block text-[12px] font-bold text-slate-700 mb-1">
                    Location / Work Site <span className="font-normal text-slate-400">(Optional)</span>
                  </label>
                  <input
                    type="text"
                    value={location}
                    onChange={(e) => setLocation(e.target.value)}
                    placeholder="e.g. Test Site, Well Pad A"
                    className="w-full bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-[12.5px] text-slate-800 focus:outline-none focus:ring-2 focus:ring-teal-500/20 focus:border-teal-600"
                  />
                </div>
              </div>

              <div>
                <label className="block text-[12px] font-bold text-slate-700 mb-1">
                  Field Note / Site Update Text
                </label>
                <textarea
                  rows={4}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  placeholder="e.g. Foundation concreting ~80% complete. Late material delivery noted."
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-[12.5px] text-slate-800 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-teal-500/20 focus:border-teal-600"
                  required
                />
              </div>

              {/* Quick sample pre-fills */}
              <div>
                <span className="text-[11px] font-semibold text-slate-400 block mb-1.5">
                  Quick Testing Samples:
                </span>
                <div className="flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    onClick={() =>
                      handlePreFill(
                        'Foundation concreting ~80% complete. Late material delivery noted.'
                      )
                    }
                    className="text-[10.5px] bg-slate-100 hover:bg-slate-200 text-slate-700 px-2 py-1 rounded transition cursor-pointer"
                  >
                    T102: 80% with Delay
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      handlePreFill('Pipeline connection segment at 70% progress.')
                    }
                    className="text-[10.5px] bg-slate-100 hover:bg-slate-200 text-slate-700 px-2 py-1 rounded transition cursor-pointer"
                  >
                    T105: 70% Pipeline
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      handlePreFill(
                        'Manifold skid welding and pipe tie-in started this morning.'
                      )
                    }
                    className="text-[10.5px] bg-slate-100 hover:bg-slate-200 text-slate-700 px-2 py-1 rounded transition cursor-pointer"
                  >
                    Ambiguous / Low-Confidence (Review Queue)
                  </button>
                </div>
              </div>

              <div className="pt-2 flex justify-end">
                <button
                  type="submit"
                  disabled={isLoading || !text.trim()}
                  className="px-5 py-2.5 bg-teal-700 hover:bg-teal-800 text-white rounded-xl font-bold text-[12px] flex items-center gap-2 shadow-sm transition disabled:opacity-50 cursor-pointer"
                >
                  {isLoading && (
                    <div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  )}
                  <span>{isLoading ? 'Processing with AI...' : 'Submit & Match to Schedule'}</span>
                </button>
              </div>
            </form>
          ) : (
            /* Spreadsheet Upload Form */
            <form onSubmit={handleFileSubmit} className="space-y-4">
              <div>
                <label className="block text-[12px] font-bold text-slate-700 mb-1">
                  Upload Mode
                </label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => setUploadMode('updates')}
                    className={`p-2.5 rounded-xl border text-[11.5px] font-bold text-left transition cursor-pointer ${
                      uploadMode === 'updates'
                        ? 'border-teal-600 bg-teal-50/50 text-teal-800'
                        : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'
                    }`}
                  >
                    <div>Site Updates Log</div>
                    <div className="text-[10px] font-normal text-slate-500">
                      Batch field updates to extract &amp; link
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() => setUploadMode('schedule')}
                    className={`p-2.5 rounded-xl border text-[11.5px] font-bold text-left transition cursor-pointer ${
                      uploadMode === 'schedule'
                        ? 'border-teal-600 bg-teal-50/50 text-teal-800'
                        : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'
                    }`}
                  >
                    <div>Master Schedule Import</div>
                    <div className="text-[10px] font-normal text-slate-500">
                      Import baseline CPM activities
                    </div>
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-[12px] font-bold text-slate-700 mb-1">
                  {uploadMode === 'schedule' ? 'Select Schedule CSV File (.csv)' : 'Select File (.csv, .xlsx, .xls)'}
                </label>
                <input
                  type="file"
                  accept={uploadMode === 'schedule' ? '.csv' : '.csv, .xlsx, .xls'}
                  onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl p-3 text-[12px] text-slate-700 file:mr-4 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-[11.5px] file:font-bold file:bg-teal-50 file:text-teal-700 hover:file:bg-teal-100"
                  required
                />
                {uploadMode === 'schedule' && (
                  <p className="mt-1.5 text-[11px] text-slate-500">
                    Schedule CSV requires columns:{' '}
                    <span className="font-semibold text-slate-700">
                      Task ID, Activity, Location, Planned Start, Planned End, Dependency
                    </span>
                  </p>
                )}
                {selectedFile && (
                  <div className="mt-1.5 text-[11px] text-slate-500">
                    Selected: <strong>{selectedFile.name}</strong> ({(selectedFile.size / 1024).toFixed(1)} KB)
                  </div>
                )}
              </div>

              {/* Schedule Baseline Replacement Option */}
              {uploadMode === 'schedule' && (
                <div className="bg-amber-50/60 border border-amber-200/80 rounded-xl p-3.5 space-y-2.5">
                  <div className="flex items-start gap-2.5">
                    <input
                      type="checkbox"
                      id="replaceScheduleCheckbox"
                      checked={replaceSchedule}
                      onChange={(e) => {
                        setReplaceSchedule(e.target.checked);
                        if (!e.target.checked) setConfirmReplace(false);
                      }}
                      className="mt-0.5 h-4 w-4 rounded border-slate-300 text-amber-600 focus:ring-amber-500 cursor-pointer"
                    />
                    <label
                      htmlFor="replaceScheduleCheckbox"
                      className="text-[12px] font-bold text-slate-800 cursor-pointer"
                    >
                      Replace Current Schedule (Clean Baseline Import)
                      <span className="block text-[11px] font-normal text-slate-600 mt-0.5">
                        Clears existing schedule activities and dependencies, establishing this file as the new project baseline.
                      </span>
                    </label>
                  </div>

                  {replaceSchedule && (
                    <div className="ml-6 pl-2.5 border-l-2 border-amber-400 space-y-2 pt-1">
                      <div className="text-[11px] text-amber-900 bg-amber-100/80 p-2 rounded-lg font-medium flex items-start gap-1.5">
                        <span className="text-amber-700 font-bold">⚠️</span>
                        <span>
                          <strong>Warning:</strong> Existing schedule tasks and dependencies will be permanently deleted. Past site updates will be unlinked and retained in your audit log.
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          id="confirmReplaceCheckbox"
                          checked={confirmReplace}
                          onChange={(e) => setConfirmReplace(e.target.checked)}
                          className="h-4 w-4 rounded border-slate-300 text-rose-600 focus:ring-rose-500 cursor-pointer"
                        />
                        <label
                          htmlFor="confirmReplaceCheckbox"
                          className="text-[11.5px] font-bold text-rose-700 cursor-pointer"
                        >
                          I confirm I want to replace the current project schedule.
                        </label>
                      </div>
                    </div>
                  )}
                </div>
              )}

              <div className="pt-2 flex justify-end">
                <button
                  type="submit"
                  disabled={
                    isLoading ||
                    !selectedFile ||
                    (uploadMode === 'schedule' && replaceSchedule && !confirmReplace)
                  }
                  className={`px-5 py-2.5 text-white rounded-xl font-bold text-[12px] flex items-center gap-2 shadow-sm transition disabled:opacity-50 cursor-pointer ${
                    uploadMode === 'schedule' && replaceSchedule
                      ? 'bg-amber-700 hover:bg-amber-800'
                      : 'bg-teal-700 hover:bg-teal-800'
                  }`}
                >
                  {isLoading && (
                    <div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  )}
                  <span>
                    {isLoading
                      ? uploadMode === 'schedule'
                        ? 'Importing Schedule...'
                        : 'Uploading & Ingesting...'
                      : uploadMode === 'schedule'
                      ? replaceSchedule
                        ? 'Replace & Import Schedule'
                        : 'Import Schedule Baseline'
                      : 'Upload & Process File'}
                  </span>
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}

