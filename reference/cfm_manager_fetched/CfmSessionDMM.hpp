#pragma once

#include "cfm_initiator.h"
#include "CfmSession.hpp"
#include "CfmSessionStats.hpp"

namespace cfm {

class CfmSessionDMM : public CfmSession
{
public:
    CfmSessionDMM(uint64_t sess_id,
                  stw_t* m_timer_wheel,
                  const SessionStartRequest& req,
                  const LMepOper& lmep_op);

    bool IsSupported() override;
    SessionStartStatus Start() override;
    SessionStopStatus Stop() override;
    SessionStopStatus Disable();
    void Wait();
    void PushOperResults() override;
    static void TimerCb(stw_tmr_t*, void* pdata);
    bool OperTestInfoInitial(corm_obj*) const override;

private:
    uint32_t hw_id;
    uint64_t ts_start;

    CfmSessionStatsDMM stats = {};
};

} // namespace cfm
