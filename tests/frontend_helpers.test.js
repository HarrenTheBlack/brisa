const assert = require('node:assert/strict');
const test = require('node:test');

const {
  buildPhysicalSensorChoices,
  configuredPhysicalSensorIds,
  escapeHtml,
  sensorReferenceKind,
  virtualSensorDependencyMessage,
} = require('../brisa/app/static/app.js');


test('retains a missing source without selecting its replacement', () => {
  const missingId = 'drive-wwid-naa.old';
  const replacementId = 'drive-wwid-naa.new';
  const config = {
    virtual_sensors: [],
    fan_configs: [],
    sensor_aliases: { [missingId]: 'Old drive' },
  };

  const choices = buildPhysicalSensorChoices(
    [{ id: replacementId, label: 'Replacement drive' }],
    [missingId],
    config
  );

  assert.deepEqual(
    choices.map(choice => [choice.id, choice.unavailable]),
    [[replacementId, false], [missingId, true]]
  );
});


test('collects configured physical IDs without virtual references', () => {
  const config = {
    virtual_sensors: [{
      id: 'virtual/max',
      source_sensor_ids: ['drive-wwid-naa.1', 'drive-wwid-naa.2'],
    }],
    fan_configs: [
      { sensor_id: 'virtual/max' },
      { sensor_id: 'drive-wwid-naa.3' },
    ],
  };

  assert.deepEqual(configuredPhysicalSensorIds(config), [
    'drive-wwid-naa.1',
    'drive-wwid-naa.2',
    'drive-wwid-naa.3',
  ]);
  assert.equal(sensorReferenceKind('virtual/missing', config), 'virtual');
});


test('explains every fan dependency before virtual deletion', () => {
  const message = virtualSensorDependencyMessage(
    { id: 'virtual/max', name: 'Max HDD temp' },
    [
      { fan_id: 'fan1', fan_label: 'Fan 1', sensor_id: 'virtual/max' },
      { fan_id: 'fan2', fan_label: 'Fan 2', sensor_id: 'virtual/max' },
    ]
  );

  assert.match(message, /Cannot delete "Max HDD temp"/);
  assert.match(message, /- Fan 1/);
  assert.match(message, /- Fan 2/);
  assert.match(message, /Reassign those fan configurations first in Fan Config/);
});


test('escapes configured values before HTML interpolation', () => {
  assert.equal(
    escapeHtml('<img src="x" onerror="bad()">'),
    '&lt;img src=&quot;x&quot; onerror=&quot;bad()&quot;&gt;'
  );
});
