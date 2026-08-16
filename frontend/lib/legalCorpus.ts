/**
 * public-domain legal text used as raw material for the text shaders.
 * (u.s. gov works - scotus opinions, constitution, federal rules - arent copyrighted)
 */
export type LegalPassage = { cite: string; text: string };

export const LEGAL_PASSAGES: LegalPassage[] = [
  {
    cite: "Graham v. Connor, 490 U.S. 386 (1989)",
    text: "The reasonableness of a particular use of force must be judged from the perspective of a reasonable officer on the scene, rather than with the 20/20 vision of hindsight. The calculus of reasonableness must embody allowance for the fact that police officers are often forced to make split-second judgments in circumstances that are tense, uncertain, and rapidly evolving about the amount of force that is necessary in a particular situation.",
  },
  {
    cite: "Marbury v. Madison, 5 U.S. 137 (1803)",
    text: "It is emphatically the province and duty of the judicial department to say what the law is. Those who apply the rule to particular cases, must of necessity expound and interpret that rule. If two laws conflict with each other, the courts must decide on the operation of each.",
  },
  {
    cite: "U.S. Const. art. III, § 1",
    text: "The judicial Power of the United States, shall be vested in one supreme Court, and in such inferior Courts as the Congress may from time to time ordain and establish. The Judges, both of the supreme and inferior Courts, shall hold their Offices during good Behaviour.",
  },
  {
    cite: "Fed. R. Civ. P. 56(a)",
    text: "The court shall grant summary judgment if the movant shows that there is no genuine dispute as to any material fact and the movant is entitled to judgment as a matter of law. The court should state on the record the reasons for granting or denying the motion.",
  },
  {
    cite: "Harlow v. Fitzgerald, 457 U.S. 800 (1982)",
    text: "Government officials performing discretionary functions generally are shielded from liability for civil damages insofar as their conduct does not violate clearly established statutory or constitutional rights of which a reasonable person would have known.",
  },
  {
    cite: "Fed. R. Evid. 702",
    text: "A witness who is qualified as an expert by knowledge, skill, experience, training, or education may testify in the form of an opinion or otherwise if the expert's scientific, technical, or other specialized knowledge will help the trier of fact to understand the evidence or to determine a fact in issue.",
  },
  {
    cite: "U.S. Const. amend. IV",
    text: "The right of the people to be secure in their persons, houses, papers, and effects, against unreasonable searches and seizures, shall not be violated, and no Warrants shall issue, but upon probable cause, supported by Oath or affirmation.",
  },
  {
    cite: "Brown v. Board of Education, 347 U.S. 483 (1954)",
    text: "We conclude that in the field of public education the doctrine of separate but equal has no place. Separate educational facilities are inherently unequal. Therefore, we hold that the plaintiffs and others similarly situated for whom the actions have been brought are, by reason of the segregation complained of, deprived of the equal protection of the laws guaranteed by the Fourteenth Amendment.",
  },
  {
    cite: "Miranda v. Arizona, 384 U.S. 436 (1966)",
    text: "Prior to any questioning, the person must be warned that he has a right to remain silent, that any statement he does make may be used as evidence against him, and that he has a right to the presence of an attorney, either retained or appointed. The defendant may waive effectuation of these rights, provided the waiver is made voluntarily, knowingly and intelligently.",
  },
  {
    cite: "Chevron U.S.A. Inc. v. NRDC, 467 U.S. 837 (1984)",
    text: "When a court reviews an agency's construction of the statute which it administers, it is confronted with two questions. First, always, is the question whether Congress has directly spoken to the precise question at issue. If the intent of Congress is clear, that is the end of the matter; for the court, as well as the agency, must give effect to the unambiguously expressed intent of Congress.",
  },
  {
    cite: "Ashcroft v. Iqbal, 556 U.S. 662 (2009)",
    text: "To survive a motion to dismiss, a complaint must contain sufficient factual matter, accepted as true, to state a claim to relief that is plausible on its face. A claim has facial plausibility when the plaintiff pleads factual content that allows the court to draw the reasonable inference that the defendant is liable for the misconduct alleged.",
  },
  {
    cite: "Erie R. Co. v. Tompkins, 304 U.S. 64 (1938)",
    text: "Except in matters governed by the Federal Constitution or by Acts of Congress, the law to be applied in any case is the law of the State. And whether the law of the State shall be declared by its Legislature in a statute or by its highest court in a decision is not a matter of federal concern. There is no federal general common law.",
  },
];

/** one long uppercase stream, passages seperated by a slash marker */
export const LEGAL_CORPUS = LEGAL_PASSAGES.map((p) => p.text)
  .join("  //  ")
  .toUpperCase();
