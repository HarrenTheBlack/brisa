const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const {
  buildPhysicalSensorChoices,
  configuredPhysicalSensorIds,
  sensorReferenceKind,
  virtualSensorDependencyMessage,
} = require('../brisa/app/static/app.js');

const STATIC_ROOT = path.join(__dirname, '..', 'brisa', 'app', 'static');
const HOSTILE = '\"><img src=x onerror=alert(1)>';


class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.classList = {
      add() {},
      remove() {},
      toggle() {},
    };
    this.textContent = '';
    this.value = '';
  }

  append(...children) {
    this.children.push(...children);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  addEventListener() {}
}


const fakeDocument = {
  createElement: tagName => new FakeElement(tagName),
  createTextNode: text => ({ nodeType: 3, textContent: text }),
};


function source(fileName) {
  return fs.readFileSync(path.join(STATIC_ROOT, fileName), 'utf8');
}


function extractFunction(fileSource, functionName, globals = {}) {
  const start = fileSource.indexOf(`function ${functionName}(`);
  assert.notEqual(start, -1, `${functionName} must remain testable`);
  const bodyStart = fileSource.indexOf('{', start);
  let depth = 0;
  let end = bodyStart;
  for (; end < fileSource.length; end += 1) {
    if (fileSource[end] === '{') depth += 1;
    if (fileSource[end] === '}') {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  return vm.runInNewContext(`(${fileSource.slice(start, end + 1)})`, globals);
}


function descendants(element) {
  return [element, ...element.children
    .filter(child => child instanceof FakeElement)
    .flatMap(descendants)];
}


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


test('constructs sensor, fan, alias, virtual, and group labels as text nodes', () => {
  const devicesSource = source('devices.html');
  const appendTextCell = extractFunction(devicesSource, 'appendTextCell', {
    document: fakeDocument,
  });
  const checkboxOption = extractFunction(devicesSource, 'checkboxOption', {
    document: fakeDocument,
  });

  const row = new FakeElement('tr');
  appendTextCell(row, HOSTILE);
  assert.equal(row.children[0].children[0].textContent, HOSTILE);

  const option = checkboxOption('group-item-option', HOSTILE, HOSTILE, HOSTILE, true);
  const optionNodes = descendants(option);
  assert.equal(optionNodes.find(node => node.tagName === 'input').value, HOSTILE);
  assert.equal(optionNodes.filter(node => node.tagName === 'div')[1].textContent, HOSTILE);
  assert.equal(optionNodes.filter(node => node.tagName === 'div')[2].textContent, HOSTILE);
});


test('constructs dashboard fan, sensor, and group payloads without HTML parsing', () => {
  const indexSource = source('index.html');
  const globals = {
    document: fakeDocument,
    cardId: prefix => `${prefix}safe`,
  };
  const fanCardElement = extractFunction(indexSource, 'fanCardElement', globals);
  const sensorCardElement = extractFunction(indexSource, 'sensorCardElement', globals);
  const createGroup = extractFunction(indexSource, 'createGroup', {
    document: fakeDocument,
    createCardGrid: () => new FakeElement('div'),
  });

  const fanCard = fanCardElement({ id: HOSTILE, label: HOSTILE });
  const sensorCard = sensorCardElement({ sensor_id: HOSTILE, alias: HOSTILE });
  const group = createGroup(HOSTILE, [], () => new FakeElement('div'));

  assert.equal(descendants(fanCard).find(node => node.className === 'stat-label').textContent, HOSTILE);
  assert.equal(descendants(sensorCard).find(node => node.className === 'stat-label').textContent, HOSTILE);
  assert.equal(descendants(group).find(node => node.className === 'dash-group-title').textContent, HOSTILE);
});


test('puts hostile curve and select labels in value and textContent properties', () => {
  const optionElement = extractFunction(source('fanconfig.html'), 'optionElement', {
    document: fakeDocument,
  });
  const curve = optionElement(HOSTILE, HOSTILE, true);

  assert.equal(curve.value, HOSTILE);
  assert.equal(curve.textContent, HOSTILE);
  assert.equal(curve.selected, true);

  const curvesSource = source('curves.html');
  assert.match(curvesSource, /name\.value = curve\.name;/);
  assert.match(curvesSource, /curve\.textContent = curveName;/);
  assert.doesNotMatch(curvesSource, /innerHTML|insertAdjacentHTML/);
});
