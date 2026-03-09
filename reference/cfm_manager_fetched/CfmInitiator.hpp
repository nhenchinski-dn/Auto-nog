#pragma once

#include <map>
#include <mutex>

#include "cfm_initiator.h"
#include "cfm_manager.h"
#include "include/stw_timer.h"
#include "Proactive.hpp"

namespace cfm {

class CfmSession;

class CfmInitiator
{
public:
    void SetTimerWheel(stw_t* timer_wheel);

    SessionStartResponse CreateSession(const SessionStartRequest& req);
    SessionStopResponse TerminateSession(const SessionStopRequest& req);

    bool StartSession(uint64_t sess_id);
    int StopSession(void* sess);

    void HandlePacket(wb_pkt* pkt);

    int SendStopSessionEvent(void* sess);
    int SendStartSessionEvent(uint64_t sess_id);

    void HandleEvents(const cfm_event_t& event);
    void DeleteMepCB(uint32_t oam_id);

    std::map<uint64_t, CfmSession*> sessions;
    std::map<uint64_t, stw_tmr_t*> timers;
    ProactiveSched pro_sched;
    stw_t* m_timer_wheel;
    std::mutex m_lock;

    inline static uint32_t sessions_allocated = 0;
    inline static uint32_t sessions_freed = 0;
};

} // namespace cfm
