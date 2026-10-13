"""
raft_consensus.py — Production-grade Raft consensus protocol implementation.

Design Rationale
================
This module implements the Raft algorithm (Ong & Ousterhout, SOSP'14) optimized
for embedded devices running on constrained hardware (target: <15MB RSS).

Why Raft over alternatives:
  - Multi-Paxos: equivalent safety guarantees but significantly harder to reason
    about (lattice-theoretic proofs vs. intuitive single-leader model). For a team
    maintaining infrastructure, understandability trumps theoretical elegance.
  - BFT-SMaR: designed for adversarial environments where nodes may lie. Our
    deployment is a trusted internal network; Byzantine fault tolerance adds
    3x communication overhead and complex view-change machinery we don't need.
  - Raft: explicit leader election, strong log-matching property, and the
    "understandability" design goal make it the right choice for production
    infrastructure that humans must debug at 3am.

Key Design Decisions:
  1. Pre-vote phase prevents partitioned nodes from stealing leadership on
     rejoin (inspired by etcd v3.4+). Without this, a node isolated for >
     election_timeout would immediately campaign and potentially disrupt a
     healthy cluster.
  2. Randomized election timeouts [150ms, 300ms] break symmetry probabilistically
     without requiring any coordination mechanism.
  3. Full snapshots (no delta compression) because our state machines are small
     (<1MB routing tables). Delta chains add decompression latency that exceeds
     the storage savings at this scale.
  4. Parallel RPC dispatch (not sequential) maximizes throughput when followers
     have heterogeneous latencies.
  5. Quorum-based commit: with N nodes, quorum = N//2 + 1. Odd cluster sizes
     guarantee liveness under floor(N/2) failures.

Safety Invariants (NEVER violated):
  - At most one leader per term
  - A voter grants at most one vote per term
  - Logs match at (index, term) => identical prefix
  - Committed entries are never overwritten
  - State machine applies entries in strictly increasing index order

Liveness Goals:
  - Leader election completes in < 500ms under single-node failure
  - Zero downtime for follower crashes
  - Majority partition retains full write throughput
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import random
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("raft")


# =============================================================================
# ENUMS
# =============================================================================

class ElectionState(Enum):
    """
    States in the Raft leader election finite state machine.

    Transitions:
      FOLLOWER --(election_timeout fires)--> PRE_VOTE_CANDIDATE
      PRE_VOTE_CANDIDATE --(pre-vote quorum achieved)--> CANDIDATE
      PRE_VOTE_CANDIDATE --(discovers higher-term active leader)--> FOLLOWER
      CANDIDATE --(vote quorum achieved)--> LEADER
      CANDIDATE --(discovers higher term)--> FOLLOWER
      LEADER --(discovers higher term in response)--> FOLLOWER
      ANY --(term updated from external source)--> FOLLOWER

    Why PRE_VOTE exists:
      A node that was partitioned away and timed out has stale information.
      If it immediately campaigns, it may win votes from other nodes that also
      lost contact with their leader, causing unnecessary disruption. Pre-vote
      asks "would you vote for me?" WITHOUT incrementing the term, so if the
      current leader is still alive, it responds negatively and the candidate
      stays a follower.
    """
    FOLLOWER = auto()
    PRE_VOTE_CANDIDATE = auto()
    CANDIDATE = auto()
    LEADER = auto()


class Role(Enum):
    """Convenience alias matching Raft terminology."""
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


# =============================================================================
# PROTOBUF-EQUIVALENT MESSAGE DATACLASSES
# =============================================================================

@dataclass(frozen=True)
class VoteRequest:
    """
    Request to cast a vote for candidacy.

    Mirrors the protobuf definition:
        message VoteRequest {
            int64 term = 1;          # Candidate's current term
            int64 candidate_id = 2;  # Who is asking for the vote
            int64 last_log_index = 3;  # Most recent log entry index
            int64 last_log_term = 4;   # Term of the most recent entry
        }

    Voting criteria (Figure 8 in the Raft paper):
      1. requester.term >= voter.currentTerm
      2. candidate's log is at least as up-to-date as voter's log
         (lastLogTerm > voter.lastLogTerm, OR equal AND lastLogIndex >= voter.lastLogIndex)
    """
    term: int
    candidate_id: str
    last_log_index: int
    last_log_term: int


@dataclass(frozen=True)
class VoteResponse:
    """
    Response to a VoteRequest.

        message VoteResponse {
            int64 term = 1;           # Current term of the responder
            bool vote_granted = 2;    # True if the voter supports the candidate
        }
    """
    term: int
    vote_granted: bool


@dataclass(frozen=True)
class PreVoteRequest:
    """
    Pre-vote probe sent before actually starting an election.

    Does NOT increment the sender's term. Used to check whether a quorum
    WOULD grant a vote, allowing the node to safely transition to CANDIDATE
    only when it can actually win.

        message PreVoteRequest {
            int64 term = 1;              # term + 1 (the term we'd campaign in)
            int64 candidate_id = 2;
            int64 last_log_index = 3;
            int64 last_log_term = 4;
        }
    """
    term: int
    candidate_id: str
    last_log_index: int
    last_log_term: int


@dataclass(frozen=True)
class PreVoteResponse:
    """
    Response to PreVoteRequest.

        message PreVoteResponse {
            int64 term = 1;             # Responder's current term
            bool pre_vote_granted = 2;
        }
    """
    term: int
    pre_vote_granted: bool


@dataclass(frozen=True)
class LogEntry:
    """
    A single entry in the replicated log.

        message LogEntry {
            int64 term = 1;       # Term when the entry was created
            int64 index = 2;      # Position in the log (1-based)
            bytes command = 3;    # Serialized client command
        }

    Entries are immutable once committed. The (term, index) pair uniquely
    identifies an entry across all replicas.
    """
    term: int
    index: int
    command: bytes = b""


@dataclass(frozen=True)
class AppendEntries:
    """
    Heartbeat / replication message from leader to followers.

    Doubles as both a heartbeat (when no log entries are included) and
    a log replication vehicle. This dual purpose minimizes round-trips:
    a periodic empty AppendEntries keeps connections alive AND detects
    dead peers, while non-empty versions replicate committed work.

        message AppendEntries {
            int64 term = 1;
            int64 leader_id = 2;
            int64 prev_log_index = 3;   # Index of entry preceding new ones
            int64 prev_log_term = 4;    # Term of that preceding entry
            repeated LogEntry entries = 5;  # New entries to replicate
            int64 leader_commit = 6;    # Leader's commit index
        }

    Consistency check (Section 5.3):
      Follower validates prev_log_index/prev_log_term against its own log.
      Mismatch triggers the consistency check: leader decrements nextIndex
      and retries with shorter prefix.
    """
    term: int
    leader_id: str
    prev_log_index: int
    prev_log_term: int
    entries: tuple[LogEntry, ...] = ()
    leader_commit: int = 0


@dataclass(frozen=True)
class AppendEntriesResponse:
    """
    Follower's acknowledgment of received log entries.

        message AppendEntriesResponse {
            int64 term = 1;
            bool success = 2;        # True if consistency check passed
            int64 match_index = 3;   # Highest matched index (if successful)
            int64 last_log_index = 4;  # Follower's last log index
            bool conflict = 5;       # True if prev_log didn't match
        }
    """
    term: int
    success: bool
    match_index: int = 0
    last_log_index: int = 0
    conflict: bool = False


@dataclass(frozen=True)
class InstallSnapshot:
    """
    Snapshot transfer for caught-up followers whose logs have been truncated.

        message InstallSnapshot {
            int64 term = 1;
            int64 leader_id = 2;
            int64 last_included_index = 3;  # Last index in the snapshot
            int64 last_included_term = 4;   # Term of that index
            bytes snapshot_data = 5;        # Compressed state machine state
            int64 offset = 6;               # Byte offset for chunked transfer
        }

    Chunked transfer (offset field) prevents memory pressure on embedded
    devices: a 10MB snapshot is sent in 1MB chunks.
    """
    term: int
    leader_id: str
    last_included_index: int
    last_included_term: int
    snapshot_data: bytes = b""
    offset: int = 0
undefined