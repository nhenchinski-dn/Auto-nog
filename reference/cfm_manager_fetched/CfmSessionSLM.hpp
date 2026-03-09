#pragma once

#include <mutex>

#include "cfm_initiator.h"
#include "CfmSession.hpp"
#include "CfmSessionStats.hpp"

namespace cfm {

class CfmSessionSLM : public CfmSession
{
public:
    CfmSessionSLM(uint64_t sess_id,
                  stw_t* m_timer_wheel,
                  const SessionStartRequest& req,
                  const LMepOper& lmep_op);

    SessionStartStatus Start() override;
    SessionStopStatus Stop() override;
    void PushOperResults() override;
    void SendPacket();
    static void TimerCb(stw_tmr_t* ptimer, void* pdata);
    void HandlePacket(wb_pkt* pkt) override;
    bool OperTestInfoInitial(corm_obj*) const override;

private:
    void UpdateLossValues();

    uint32_t test_id;

    CfmSessionStatsSLM local_stats = {};
    uint32_t min_remote_tx = UINT32_MAX;
    uint32_t max_remote_tx = 0;
    bool tx_done;
    bool rx_done;
    std::mutex m_lock;
};

} // namespace cfm
