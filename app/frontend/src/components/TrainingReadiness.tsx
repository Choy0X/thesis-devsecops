import type {DataQualification, SecurityReadiness, TrainingAssessment} from '../lib/api';

const MECHANISMS = ['prototype_pollution', 'path_traversal', 'command_injection'];
const PARTITIONS = ['train', 'selection', 'calibration', 'untouched_test'];
const isOperational = (readiness?: SecurityReadiness | null, assessment?: TrainingAssessment | null) =>
  readiness?.schema === 'security-readiness/4' || readiness?.qualification_policy === 'qualified-npm/1' ||
  assessment?.qualification_policy === 'qualified-npm/1' || !!readiness?.data_qualification || !!assessment?.data_qualification;
const qualificationReason = (reason: string) => reason === 'insufficient_validated_vulnerability_fix_families' || reason.startsWith('insufficient_qualified_source_groups:');

function qualificationPassed(report?: DataQualification) {
  if (!report || report.schema !== 'operational-data-qualification/1' || report.policy !== 'qualified-npm/1' ||
      report.sampling_unit !== 'independent_source_family' || report.qualified !== true || report.blockers?.length ||
      !Number.isInteger(report.validated_families) || report.validated_families < 1000 ||
      report.requirements?.validated_families !== 1000 || report.requirements.train_groups_per_mechanism !== 50 ||
      report.requirements.held_out_groups_per_mechanism_per_role !== 10 ||
      MECHANISMS.some(mechanism => !report.requirements.mechanisms?.includes(mechanism))) return false;
  return PARTITIONS.every(role => MECHANISMS.every(mechanism => {
    const count = report.partition_mechanism_groups?.[role]?.[mechanism];
    return Number.isInteger(count) && count >= (role === 'train' ? 50 : 10) && count <= report.validated_families;
  }));
}

export function fullTrainingBlocked(readiness?: SecurityReadiness | null, assessment?: TrainingAssessment | null) {
  if (isOperational(readiness, assessment)) {
    return !qualificationPassed(readiness?.data_qualification ?? assessment?.data_qualification) ||
      assessment?.allowed === false || readiness?.training_ready === false || !!readiness?.blockers?.length;
  }
  return assessment ? !assessment.allowed : readiness?.training_ready !== true || !!readiness.blockers?.length ||
    (readiness.scale_assessment?.schema === 'security-corpus-scale/1' && readiness.scale_assessment.full_run_suitability === 'pilot_only');
}

export function TrainingReadiness({readiness, assessment, purpose = 'train'}: {readiness?: SecurityReadiness | null; assessment?: TrainingAssessment | null; purpose?: string}) {
  if (purpose === 'smoke') return <p className="text-[0.78rem] text-muted">Software smoke diagnostic. This does not establish security quality.</p>;
  if (!readiness && !assessment) return <p role="status" className="text-[0.78rem] text-muted">Checking training readiness...</p>;
  const blocked = fullTrainingBlocked(readiness, assessment);
  const operational = isOperational(readiness, assessment);
  const qualification = readiness?.data_qualification ?? assessment?.data_qualification;
  const qualified = qualificationPassed(qualification);
  const integrity = assessment?.integrity.state ?? (readiness?.training_ready == null ? 'not assessed' :
    readiness.training_ready || (operational && (readiness.blockers ?? []).every(qualificationReason)) ? 'passed' : 'blocked');
  const performanceDesign = readiness?.security_performance_design ?? readiness?.scale_assessment ?? assessment?.security_performance_design;
  const limitations = operational ? performanceDesign?.limitations ?? [] : assessment?.full_run.limitations ?? performanceDesign?.limitations ?? [];
  const reasons = [...new Set([...(assessment?.blockers ?? readiness?.blockers ?? []), ...(qualification?.blockers ?? [])])];
  return <section aria-label="Training readiness" className="space-y-2 border border-line bg-surface-2 p-3 text-[0.78rem]">
    <p className={blocked ? 'font-medium text-warn' : 'font-medium'}>{blocked ? 'Built, insufficient evidence for full training' : 'Dataset evidence checks passed'}</p>
    <dl className="flex flex-wrap gap-x-5 gap-y-1 text-muted">
      <div><dt className="inline">Dataset integrity: </dt><dd className="inline">{integrity}</dd></div>
      {operational && <div><dt className="inline">Operational qualification: </dt><dd className="inline">{qualified ? 'passed' : qualification ? 'blocked' : 'missing'}</dd></div>}
      <div><dt className="inline">Full-run data: </dt><dd className="inline">{blocked ? 'blocked' : 'passed'}</dd></div>
      <div><dt className="inline">Correctness diagnostics: </dt><dd className="inline">{assessment?.diagnostics.state ?? (blocked ? 'blocked' : 'pending')}</dd></div>
      <div><dt className="inline">Security performance: </dt><dd className="inline">{assessment?.security_performance?.state ?? "unmeasured"}</dd></div>
    </dl>
    {operational && <>
      <p><span>Qualified vulnerability/fix families:</span> <span className="tabular-nums">{qualification?.validated_families ?? 'Not reported'} / 1000</span></p>
      <details><summary className="cursor-pointer text-muted">Operational qualification details</summary><div className="mt-2 space-y-2">
        <p className="text-muted">Distinct validated vulnerability/fix families. Source groups keep related repositories and code together. Each mechanism needs 50 training groups and 10 groups in each held-out role.</p>
        {!qualification && <p className="text-warn">No operational qualification report was recorded. Rebuild with the current qualification policy.</p>}
        {qualification && <>
          <div className="overflow-x-auto"><table className="w-full text-left" aria-label="Operational mechanism requirements"><thead className="text-muted"><tr><th className="pr-3 font-medium">Role</th><th className="pr-3 font-medium">Mechanism</th><th className="pr-3 font-medium">Source groups</th><th className="pr-3 font-medium">Required</th><th className="font-medium">Shortage</th></tr></thead><tbody>{PARTITIONS.flatMap(role => MECHANISMS.map(mechanism => {
            const count = qualification.partition_mechanism_groups?.[role]?.[mechanism];
            const required = role === 'train' ? 50 : 10;
            return <tr key={`${role}:${mechanism}`} className="border-t border-line"><td className="py-1 pr-3">{role.replace(/_/g, ' ')}</td><td className="pr-3">{mechanism.replace(/_/g, ' ')}</td><td className="pr-3 tabular-nums">{count ?? 'Not reported'}</td><td className="pr-3 tabular-nums">{required}</td><td className="tabular-nums">{count == null ? 'Not reported' : Math.max(0, required - count)}</td></tr>;
          }))}</tbody></table></div>
          {!!Object.keys(qualification.excluded_records ?? {}).length && <div><p className="font-medium">Qualification exclusions</p><ul className="mt-1 space-y-1 text-muted">{Object.entries(qualification.excluded_records ?? {}).map(([reason, count]) => <li key={reason}>{reason.replace(/_/g, ' ')}: {count}</li>)}</ul></div>}
        </>}
      </div></details>
    </>}
    {!!limitations.length && <details><summary className="cursor-pointer text-muted">Statistical performance design</summary><div className="mt-2 overflow-x-auto"><table className="w-full text-left" aria-label="Security partition requirements">
      <thead className="text-muted"><tr><th className="pr-3 font-medium">Partition</th><th className="pr-3 font-medium">Pool</th><th className="pr-3 font-medium">Source groups</th><th className="pr-3 font-medium">Design target</th><th className="font-medium">Shortage</th></tr></thead>
      <tbody>{limitations.map(row => <tr key={`${row.partition}:${row.pool}`} className="border-t border-line"><td className="py-1 pr-3">{row.partition.replace(/_/g, ' ')}</td><td className="pr-3">{row.pool === 'scoped_negative' ? 'protected' : row.pool}</td><td className="pr-3 tabular-nums">{row.source_groups}</td><td className="pr-3 tabular-nums">{row.zero_error_design_groups}</td><td className="tabular-nums">{Math.max(0, row.zero_error_design_groups - row.source_groups)}</td></tr>)}</tbody>
    </table><p className="mt-1 text-muted">These statistical claim design targets are separate from operational training qualification. Counts do not establish measured model performance or statistical independence.</p></div></details>}
    {!!reasons.length && <details><summary className="cursor-pointer text-muted">Readiness details</summary><ul className="mt-2 space-y-1">{reasons.map(reason => <li key={reason} className="break-words">{reason.replace(/_/g, ' ')}</li>)}</ul></details>}
    {!blocked && <p className="text-muted">Matching model correctness checks must pass before full training. Security performance requires independent held-out evaluation.</p>}
  </section>;
}
