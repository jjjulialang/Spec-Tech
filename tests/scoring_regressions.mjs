import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { deriveAnchors, grade, parseOutput } from "../src/grade.ts";


const practiceManifest = JSON.parse(
  readFileSync(new URL("../examples/practice-dataset/manifest.json", import.meta.url), "utf8"),
);
const hardCases = JSON.parse(
  readFileSync(new URL("./hard_dataset/cases.json", import.meta.url), "utf8"),
);

const practiceReports = [
  {
    document: "schedule.pdf",
    category: "cross-document-conflict",
    location: "PDF page 1, door D-202",
    description: "D-202 is rated 45 min at Mechanical 101 instead of the required 90 minutes.",
  },
  {
    document: "schedule.pdf",
    category: "unit-error",
    location: "PDF page 1, fixture L-1",
    description: "L-1 is scheduled at 5.0 gpm instead of the specified 0.5 gpm.",
  },
];


test("practice reports achieve exact perfect score", () => {
  assert.deepEqual(grade(practiceManifest, practiceReports), {
    reported: 2,
    matched: 2,
    precision: 1,
    recall: 1,
    f1: 1,
  });
});


test("one duplicate lowers precision without increasing recall", () => {
  const reports = [...practiceReports, { ...practiceReports[1] }];
  const score = grade(practiceManifest, reports);
  assert.equal(score.matched, 2);
  assert.equal(score.precision, 2 / 3);
  assert.equal(score.recall, 1);
  assert.equal(score.f1, 0.8);
});


test("wrong document and wrong category cannot match strong keywords", () => {
  const reports = [
    { ...practiceReports[0], document: "spec.pdf" },
    { ...practiceReports[1], category: "cross-document-conflict" },
  ];
  assert.deepEqual(grade(practiceManifest, reports), {
    reported: 2,
    matched: 0,
    precision: 0,
    recall: 0,
    f1: 0,
  });
});


test("recall loss and over-reporting have predictable F1 penalties", () => {
  const missingOne = grade(practiceManifest, [practiceReports[0]]);
  assert.equal(missingOne.precision, 1);
  assert.equal(missingOne.recall, 0.5);
  assert.equal(missingOne.f1, 2 / 3);

  const falsePositive = {
    document: "schedule.pdf",
    category: "missing-item",
    location: "PDF page 1, invented DF-9",
    description: "DF-9 is allegedly missing, but this report has no supporting requirement.",
  };
  const spammed = grade(practiceManifest, [...practiceReports, falsePositive]);
  assert.equal(spammed.precision, 2 / 3);
  assert.equal(spammed.recall, 1);
  assert.equal(spammed.f1, 0.8);
});


test("hard fixture punishes both a duplicate and an unsupported distractor", () => {
  const expected = hardCases.expected_output.errors;
  assert.equal(grade(hardCases.manifest, expected).f1, 1);

  const duplicateAndDistractor = [
    ...expected,
    { ...expected[0] },
    {
      document: "M-201_mechanical-plan.pdf",
      category: "unit-error",
      location: "PDF page 1, CHW-2",
      description: "CHW-2 says 2 in while another note says the equivalent 50.8 mm.",
    },
  ];
  const score = grade(hardCases.manifest, duplicateAndDistractor);
  assert.equal(score.matched, 8);
  assert.equal(score.reported, 10);
  assert.equal(score.precision, 0.8);
  assert.equal(score.recall, 1);
  assert.ok(Math.abs(score.f1 - 8 / 9) < Number.EPSILON);
});


test("a verifier may legitimately remove more than half of noisy discovery", () => {
  const manifest = { errors: hardCases.manifest.errors.slice(0, 3) };
  const correct = hardCases.expected_output.errors.slice(0, 3);
  const unsupported = Array.from({ length: 5 }, (_, index) => ({
    document: "project-specifications.pdf",
    category: "code-violation",
    location: `PDF page 1, unsupported item U-${index}`,
    description: `Unsupported item U-${index} is speculative and has no controlling evidence.`,
  }));
  const noisyDiscovery = grade(manifest, [...correct, ...unsupported]);
  const conservativeVerification = grade(manifest, correct);
  assert.equal(noisyDiscovery.matched, 3);
  assert.equal(noisyDiscovery.f1, 6 / 11);
  assert.equal(conservativeVerification.f1, 1);
});


test("page number is an OR-match and can be satisfied by an unrelated value", () => {
  const manifest = {
    errors: [
      {
        document: "plan.pdf",
        category: "unit-error",
        page: 7,
        keywords: ["AHU-77"],
      },
    ],
  };
  const unrelatedNumber = [
    {
      document: "plan.pdf",
      category: "unit-error",
      location: "unknown page",
      description: "A completely different pipe is shown as 7 inches.",
    },
  ];
  assert.equal(grade(manifest, unrelatedNumber).matched, 1);
});


test("greedy matching makes report order significant for overlapping anchors", () => {
  const manifest = {
    errors: [
      { document: "plan.pdf", category: "missing-item", keywords: ["ROOM-1"] },
      { document: "plan.pdf", category: "missing-item", keywords: ["EF-2"] },
    ],
  };
  const broad = {
    document: "plan.pdf",
    category: "missing-item",
    location: "ROOM-1",
    description: "ROOM-1 is missing EF-2 according to the schedule.",
  };
  const roomOnly = {
    document: "plan.pdf",
    category: "missing-item",
    location: "ROOM-1",
    description: "ROOM-1 is missing its explicitly required item.",
  };
  assert.equal(grade(manifest, [broad, roomOnly]).matched, 1);
  assert.equal(grade(manifest, [roomOnly, broad]).matched, 2);
});


test("output parser counts schema-shaped objects but agent cleanup must filter them", () => {
  const parsed = parseOutput(
    JSON.stringify({
      errors: [
        practiceReports[0],
        null,
        "junk",
        { document: 42, category: [], description: {} },
      ],
    }),
  );
  assert.ok("errors" in parsed);
  // null and primitive entries are skipped; the object with wrong field types
  // remains as an empty report and would lower precision if production emitted it.
  assert.equal(parsed.errors.length, 2);
  assert.equal(parsed.errors[1].document, undefined);
});


test("derived anchors retain numeric construction identifiers", () => {
  const anchors = deriveAnchors({
    document: "plan.pdf",
    category: "cross-document-conflict",
    location: "sheet S102, detail 3/A501",
    description: "Equipment CG-1 is 35 kVA with a 3/4 inch connection.",
  });
  assert.deepEqual(anchors, ["S102", "3/A501", "CG-1", "35", "3/4"]);
});
