import type { SearchResult } from "@/lib/results";

/**
 * Placeholder results until the runtime is wired up. File URLs point at
 * bundled samples in /public/samples; the real ones will be absolute URLs on
 * the DGX Spark.
 */
export const DUMMY_RESULTS: SearchResult[] = [
  {
    id: "c-3391841",
    title: "Estate of Lopez v. City of Fresno",
    subtitle: "9th Cir. · Mar 4, 2021 · No. 19-15566",
    score: 0.94,
    highlights: [
      "Cites Graham v. Connor, 490 U.S. 386",
      "Exhibit 12 is a dash-cam still of the traffic stop",
      "Judge Nguyen: “I've watched it several times. I'm skeptical.”",
    ],
    files: [
      {
        url: "/samples/oral-argument.wav",
        title: "Oral argument",
        field: "docket.argument",
        timestamp: 2.5,
        snippet:
          "…the footage shows both hands on the steering wheel at the moment the first shot is fired. How do you square that?",
      },
      {
        url: "/samples/exhibit-12.png",
        title: "Exhibit 12 — dash-cam still",
        field: "cluster.scan_pages",
        page: 47,
      },
      {
        url: "/samples/opinion.pdf",
        title: "Opinion (slip)",
        field: "opinion.plain_text",
        page: 2,
        size: 1982,
      },
      {
        url: "/samples/argument-transcript.txt",
        title: "Argument transcript",
        field: "docket.argument_transcript",
        snippet: "JUDGE NGUYEN: I've watched it several times. I'm skeptical.",
      },
    ],
  },
  {
    id: "c-2884120",
    title: "Hernandez v. Town of Gilbert",
    subtitle: "9th Cir. · Sep 22, 2018 · No. 16-16994",
    score: 0.81,
    highlights: [
      "Cites Graham v. Connor, 490 U.S. 386",
      "Scanned record includes body-camera frame (Ex. 4)",
    ],
    files: [
      {
        url: "/samples/exhibit-12.png",
        title: "Exhibit 4 — body-cam frame",
        field: "cluster.scan_pages",
      },
      {
        url: "/samples/opinion.pdf",
        title: "Memorandum disposition",
        field: "opinion.plain_text",
        size: 1982,
      },
    ],
  },
  {
    id: "c-2410077",
    title: "S.B. v. County of San Diego",
    subtitle: "9th Cir. · Jul 15, 2017 · No. 15-56848",
    score: 0.73,
    highlights: [
      "Cites Graham v. Connor, 490 U.S. 386",
      "Panel questioned the timing of the second shot at argument",
    ],
    files: [
      {
        url: "/samples/oral-argument.wav",
        title: "Oral argument",
        field: "docket.argument",
        timestamp: 1.0,
      },
      {
        url: "/samples/argument-transcript.txt",
        title: "Argument transcript",
        field: "docket.argument_transcript",
      },
    ],
  },
];
