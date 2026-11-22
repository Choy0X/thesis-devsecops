import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Button, ErrorState, Panel, Skeleton } from "../components/ui";
import { IconButton, SettingFacts, SettingNote, SettingsCard } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { useOnActivate } from "../components/ActivePage";
import { api, errorText, type DbBackup, type DbOverview, type DbStatus } from "../lib/api";
import { num } from "../lib/format";

// Records live in an embedded local SQLite file.
const mb = (bytes: number) => (bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(0, Math.round(bytes / 1e3))} kB`);

export function Database() {
  const [st, setSt] = useState<DbStatus | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const load = () => api.dbStatus().then((s) => { setSt(s); setErr(null); }).catch(setErr);
  useEffect(() => { load(); }, []);
  useOnActivate(load);
  if (err && !st) return <ErrorState error={err} onRetry={load} title="Could not read the database status" />;
  if (!st) return <Skeleton className="h-40 w-full" />;
  return (
    <div className="space-y-5">
      <Connection st={st} reload={load} />
      {st.reachable && <Contents />}
      {st.reachable && <Backups />}
    </div>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return <><dt>{k}</dt><dd className="font-medium">{children}</dd></>;
}

function Connection({ st, reload }: { st: DbStatus; reload: () => void }) {
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const run = async (what: "schema") => {
    setBusy(what);
    setNote("");
    try {
      await api.dbEnsureSchema();
      await reload();
    } catch (e) {
      setNote(errorText(e));
    } finally {
      setBusy("");
    }
  };
  const missing = st.collections ? st.collections.expected - st.collections.present : 0;
  return (
    <SettingsCard as="h2" title="Local database"
      desc="Your records are stored in a SQLite file on this computer. The app opens it automatically."
      action={<IconButton label="Check again" onClick={reload}><RefreshCw size={15} aria-hidden /></IconButton>}
      footer={st.reachable && missing > 0 ? (
        <>
          {st.reachable && missing > 0 && (
            <Button variant="primary" loading={busy === "schema"} onClick={() => run("schema")}>Create the {missing} missing collection(s) and indexes</Button>
          )}
        </>) : undefined}>
      <SettingFacts label="The local database">
        <Row k="File"><span className="break-all font-mono">{st.path || st.uri}</span></Row>
        <Row k="Database">{st.database}</Row>
        <Row k="Ready">{st.reachable ? <span className="text-ok">yes</span> : <span className="text-crit">no{st.reason ? ` (${st.reason})` : ""}</span>}</Row>
        {st.reachable && <>
          <Row k="Engine">SQLite {st.version}</Row>
          <Row k="Writes">{st.journal_mode?.toUpperCase() || "WAL"} (write-ahead log)</Row>
          <Row k="Search">Text search and exact local vector search</Row>
          <Row k="Schema">{st.collections ? `${st.collections.present} of ${st.collections.expected} collections` : "-"}{st.schema_version != null ? `, version ${st.schema_version}` : ""}</Row>
        </>}
      </SettingFacts>
      {st.reachable && missing === 0 && <SettingNote className="text-faint">Every collection of the data model exists.</SettingNote>}
      {!st.reachable && <SettingNote className="text-crit">Check this file's folder permissions and available disk space, then check again.</SettingNote>}
      {note && <SettingNote className="text-crit">{note}</SettingNote>}
    </SettingsCard>
  );
}

function Contents() {
  const [ov, setOv] = useState<DbOverview | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const load = () => api.dbOverview().then((o) => { setOv(o); setErr(null); }).catch(setErr);
  useEffect(() => { load(); }, []);
  useOnActivate(load);
  if (err && !ov) return <ErrorState error={err} onRetry={load} title="Could not read the collections" />;
  if (!ov) return <Skeleton className="h-40 w-full" />;
  return (
    <Panel title="Contents" desc={`${num(ov.documents)} documents, ${mb(ov.bytes)} of data in ${ov.database}. The database starts empty: collect data on the Collection page.`}>
      <div className="overflow-x-auto">
        <table className="w-full text-[0.8rem]" aria-label="Collections of the database">
          <thead><tr className="text-left text-[0.72rem] uppercase tracking-wide text-faint">
            <th className="py-1.5 pr-4 font-medium">Collection</th><th className="py-1.5 pr-4 text-right font-medium">Documents</th>
            <th className="py-1.5 pr-4 text-right font-medium">Data</th><th className="py-1.5 pr-4 text-right font-medium">Indexes</th>
            <th className="py-1.5 font-medium">Holds</th></tr></thead>
          <tbody>
            {ov.collections.map((c) => (
              <tr key={c.name} className="border-t border-line">
                <td className="py-1.5 pr-4 font-mono text-[0.76rem]">{c.name}</td>
                <td className="py-1.5 pr-4 text-right tabular-nums">{num(c.documents)}</td>
                <td className="py-1.5 pr-4 text-right tabular-nums">{mb(c.bytes)}</td>
                <td className="py-1.5 pr-4 text-right tabular-nums text-muted">{c.indexes} / {mb(c.index_bytes)}</td>
                <td className="py-1.5 text-muted">{c.what}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function Backups() {
  const [list, setList] = useState<{ backups: DbBackup[]; folder: string } | null>(null);
  const load = () => api.dbBackups().then(setList).catch(() => undefined);   // ignored: the list keeps its last state
  useEffect(() => { load(); }, []);
  useOnActivate(load);
  return (
    <Panel title="Backups"
      desc="A backup is a consistent copy of the local database, checked for integrity and recorded with a checksum. Restore into a separate database file before switching to it while the app is closed.">
      <RunConsole stage="db_backup" label="Back up now" params={{}} onDone={load}
        hint="Takes seconds for a small database and minutes for a collected one. The archive lands in the backups folder." />
      {list && list.backups.length > 0 && (
        <ul className="mt-4 space-y-1 text-[0.78rem]">
          {list.backups.slice(0, 8).map((b) => (
            <li key={b.file} className="flex flex-wrap items-center gap-x-4 gap-y-0.5">
              <span className="font-mono text-[0.74rem]">{b.file}</span>
              <span className="text-muted">{mb(b.bytes)}{b.documents != null ? `, ${num(b.documents)} documents` : ""}</span>
              <span className={b.verified ? "text-ok" : "text-warn"}>{b.verified ? "verified" : "not verified"}</span>
            </li>
          ))}
        </ul>
      )}
      {list && <p className="mt-3 text-[0.74rem] text-faint">Folder: <span className="font-mono">{list.folder}</span></p>}
    </Panel>
  );
}
