import {fireEvent, render, screen, within} from '@testing-library/react';
import {expect, test} from 'vitest';
import {TrainingReadiness, fullTrainingBlocked} from './TrainingReadiness';

test('structurally ready pilot data does not claim full training is ready', () => {
  const readiness = {training_ready: true, scale_assessment: {schema: 'security-corpus-scale/1', full_run_suitability: 'pilot_only',
    limitations: [{partition: 'calibration', pool: 'supported', source_groups: 2, zero_error_design_groups: 29}]}};
  expect(fullTrainingBlocked(readiness)).toBe(true);
  render(<TrainingReadiness readiness={readiness} />);
  expect(screen.getByText('Built, insufficient evidence for full training')).toBeInTheDocument();
  fireEvent.click(screen.getByText('Statistical performance design'));
  expect(screen.getByRole('table')).toHaveTextContent('calibration');
  expect(screen.getByRole('table')).toHaveTextContent('27');
  expect(screen.getByText('Correctness diagnostics:').parentElement).toHaveTextContent('blocked');
});

test('ready data still explains that model diagnostics are pending', () => {
  render(<TrainingReadiness readiness={{training_ready: true}} />);
  expect(screen.getByText('Full-run data:').parentElement).toHaveTextContent('passed');
  expect(screen.getByText('Correctness diagnostics:').parentElement).toHaveTextContent('pending');
});


const qualifiedReport = (qualified = false) => ({schema: 'operational-data-qualification/1', policy: 'qualified-npm/1', sampling_unit: 'independent_source_family', qualified, validated_families: qualified ? 1000 : 12,
  requirements: {validated_families: 1000, train_groups_per_mechanism: 50, held_out_groups_per_mechanism_per_role: 10, mechanisms: ['prototype_pollution', 'path_traversal', 'command_injection']},
  partition_mechanism_groups: Object.fromEntries(['train', 'selection', 'calibration', 'untouched_test'].map(role => [role, Object.fromEntries(['prototype_pollution', 'path_traversal', 'command_injection'].map(mechanism => [mechanism, qualified ? (role === 'train' ? (mechanism === 'prototype_pollution' ? 810 : 50) : 10) : 1]))])),
  blockers: qualified ? [] : ['insufficient_validated_vulnerability_fix_families'], excluded_records: {unvalidated_or_unsupported_pair: 8}});

test('shows operational family and mechanism shortages with separate unmeasured performance', () => {
  const readiness = {schema: 'security-readiness/4', training_ready: true, qualification_policy: 'qualified-npm/1', data_qualification: qualifiedReport()};
  expect(fullTrainingBlocked(readiness)).toBe(true);
  render(<TrainingReadiness readiness={readiness} />);
  expect(screen.getByText('Qualified vulnerability/fix families:').parentElement).toHaveTextContent('12 / 1000');
  expect(screen.getByText('Built, insufficient evidence for full training')).toBeInTheDocument();
  expect(screen.getByText('Security performance:').parentElement).toHaveTextContent('unmeasured');
  const summary = screen.getByText('Operational qualification details');
  expect(summary.closest('details')).not.toHaveAttribute('open');
  fireEvent.click(summary);
  const table = screen.getByRole('table', {name: 'Operational mechanism requirements'});
  const rows = within(table).getAllByRole('row');
  expect(rows).toHaveLength(13);
  expect(rows[1]).toHaveTextContent('train'); expect(rows[1]).toHaveTextContent('50'); expect(rows[1]).toHaveTextContent('49');
  expect(rows[4]).toHaveTextContent('selection'); expect(rows[4]).toHaveTextContent('10'); expect(rows[4]).toHaveTextContent('9');
});

test('qualified operational data does not inherit old statistical claim floors as training blockers', () => {
  const readiness = {schema: 'security-readiness/4', training_ready: true, qualification_policy: 'qualified-npm/1', data_qualification: qualifiedReport(true),
    scale_assessment: {schema: 'security-corpus-scale/1', full_run_suitability: 'pilot_only', limitations: [{partition: 'calibration', pool: 'supported', source_groups: 10, zero_error_design_groups: 29}]}};
  expect(fullTrainingBlocked(readiness)).toBe(false);
  render(<TrainingReadiness readiness={readiness} />);
  expect(screen.getByText('Full-run data:').parentElement).toHaveTextContent('passed');
  expect(screen.getByText('Correctness diagnostics:').parentElement).toHaveTextContent('pending');
  expect(screen.getByText('Security performance:').parentElement).toHaveTextContent('unmeasured');
  expect(screen.getByText('Statistical performance design').closest('details')).not.toHaveAttribute('open');
  expect(screen.getByText('Operational qualification details').closest('details')).not.toHaveAttribute('open');
});

test('an operational declaration without qualification evidence remains blocked', () => {
  expect(fullTrainingBlocked({schema: 'security-readiness/4', training_ready: true, qualification_policy: 'qualified-npm/1'})).toBe(true);
});


test('contradictory operational counts cannot turn an allowed assessment green', () => {
  const report = qualifiedReport(true);
  report.partition_mechanism_groups.train.prototype_pollution = 1001;
  expect(fullTrainingBlocked({schema: 'security-readiness/4', training_ready: true, data_qualification: report})).toBe(true);
});

test('distinct validated families may share conservative source groups', () => {
  const report = qualifiedReport(true);
  report.partition_mechanism_groups.train.prototype_pollution = 50;
  expect(fullTrainingBlocked({schema: 'security-readiness/4', training_ready: true, data_qualification: report})).toBe(false);
});
