from scripts.run_phase4_acceptance import Phase4AcceptanceSupervisor


def test_phase4_acceptance_supervisor_uses_snapshot_artifact_refs() -> None:
    supervisor = Phase4AcceptanceSupervisor()
    initial = supervisor.decide({"nodes": [], "artifacts": []}).decision
    assert len(initial.create_tasks) == 2

    evidence = [
        {
            "artifact_ref": "N1.evidence@v1",
            "artifact_type": "evidence",
        },
        {
            "artifact_ref": "N2.evidence@v1",
            "artifact_type": "evidence",
        },
    ]
    nodes = [
        {"node_id": "N1", "status": "SUCCEEDED"},
        {"node_id": "N2", "status": "SUCCEEDED"},
    ]
    diagnosis = supervisor.decide({"nodes": nodes, "artifacts": evidence}).decision

    assert len(diagnosis.create_tasks) == 2
    assert all(
        task.input_artifact_ids == ("N1.evidence@v1", "N2.evidence@v1")
        for task in diagnosis.create_tasks
    )
