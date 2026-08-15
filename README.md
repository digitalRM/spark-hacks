# Amicus.
Amicus is a compiler which converts natural language into a repeatable, interpretable query language that operates on multimodal data -- audio data, visual data, document data, and structured data. To accomplish this, we need five stages.
1. *Data ingestion* creates schemas for the multimodal data.
2. *Frontend* presents a UI.
3. *Query language* compiles the NL to an AST.
4. *Optimizer* converts the AST to an execution plan.
5. *Runtime* runs the execution plan.