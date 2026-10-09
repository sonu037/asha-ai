# ASHA AI v0.2.1 — End-to-End Test Milestone

This milestone adds an automated core-workflow test.

Run:
```powershell
python -m pytest
```

The new test exercises:
health -> household -> person -> document -> extraction -> field verification -> event -> timeline -> person retrieval.

This is still a development baseline. The AI provider remains the deterministic mock provider from v0.2. Real OCR/vision and voice are next.
