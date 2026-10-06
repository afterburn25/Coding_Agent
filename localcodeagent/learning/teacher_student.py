"""Teacher/student learning pipeline (Part 38).

Model-agnostic orchestration over callables the host supplies:

    hard verified problem
      → teacher_fn(problem)   deep/strong model solves
      → verify_fn(solution)   tests/verification pass?
      → review_fn(solution)   critic checks
      → student_fn(problem)   smaller model attempts
      → score

Only verified outcomes flow into the TrainingCandidateGate and the
CompetencyMap — a teacher's unverified draft is never training data.
"""

from __future__ import annotations

import time

from . import taxonomy as t


class TeacherStudentPipeline:
    def __init__(self, *, gate=None, competencies=None,
                 min_score: float = 0.7) -> None:
        self.gate = gate
        self.competencies = competencies
        self.min_score = float(min_score)

    def run(
        self,
        problem: str,
        *,
        competency: str = "general",
        teacher_fn=None,
        student_fn=None,
        verify_fn=None,
        review_fn=None,
    ) -> dict:
        result = {"problem": problem[:400], "competency": competency,
                  "stages": [], "training_candidate": None,
                  "teacher_ok": False, "student_score": None,
                  "ts": time.time()}

        teacher_sol = teacher_fn(problem) if callable(teacher_fn) else ""
        result["stages"].append({"stage": "teacher", "ok": bool(teacher_sol)})
        if not teacher_sol:
            result["error"] = "teacher produced no solution"
            return result

        verified = bool(verify_fn(problem, teacher_sol)) \
            if callable(verify_fn) else False
        result["stages"].append({"stage": "verify", "ok": verified})
        if not verified:
            result["error"] = "teacher solution failed verification"
            return result

        critique = review_fn(problem, teacher_sol) \
            if callable(review_fn) else ""
        result["stages"].append({"stage": "review",
                                 "ok": True, "critique": str(critique)[:400]})
        result["teacher_ok"] = True

        student_score = None
        if callable(student_fn):
            student_sol = student_fn(problem)
            student_ok = bool(verify_fn(problem, student_sol)) \
                if callable(verify_fn) else bool(student_sol)
            student_score = 1.0 if student_ok else 0.0
            result["stages"].append(
                {"stage": "student", "ok": student_ok,
                 "score": student_score})
            result["student_score"] = student_score

        if self.competencies is not None:
            self.competencies.record(competency, "success",
                                     quality=1.0, model="teacher")
            if student_score is not None:
                self.competencies.record(
                    competency, "success" if student_score >= self.min_score
                    else "failure", quality=student_score, model="student")

        if self.gate is not None:
            cand = self.gate.submit(
                instruction=problem[:4000],
                response=str(teacher_sol)[:8000],
                kind="teacher_verified",
                source_type=t.SOURCE_TEST,
                verification=t.VERIFY_TESTED,
                confidence=0.8,
                provenance={"pipeline": "teacher_student",
                            "competency": competency,
                            "student_score": student_score,
                            "critique": str(critique)[:300]},
                negative_example=student_score is not None
                and student_score < self.min_score)
            result["training_candidate"] = bool(cand)
        return result
