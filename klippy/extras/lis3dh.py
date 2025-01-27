# Support for reading acceleration data from an LIS3DH chip
#
# Copyright (C) 2023  Zhou.XianMing <zhouxm@biqu3d.com>
# Copyright (C) 2020-2023  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import bus, adxl345, bulk_sensor

# LIS3DH registers
REG_LIS3DH_WHO_AM_I_ADDR = 0x0F
REG_LIS3DH_CTRL_REG1_ADDR = 0x20
REG_LIS3DH_CTRL_REG2_ADDR = 0x21
REG_LIS3DH_CTRL_REG3_ADDR = 0x22
REG_LIS3DH_CTRL_REG4_ADDR = 0x23
REG_LIS3DH_CTRL_REG5_ADDR = 0x24
REG_LIS3DH_CTRL_REG6_ADDR = 0x25
REG_LIS3DH_STATUS_REG_ADDR = 0x27
REG_LIS3DH_OUT_XL_ADDR = 0x28
REG_LIS3DH_OUT_XH_ADDR = 0x29
REG_LIS3DH_OUT_YL_ADDR = 0x2A
REG_LIS3DH_OUT_YH_ADDR = 0x2B
REG_LIS3DH_OUT_ZL_ADDR = 0x2C
REG_LIS3DH_OUT_ZH_ADDR = 0x2D
REG_LIS3DH_FIFO_CTRL   = 0x2E
REG_LIS3DH_FIFO_SAMPLES = 0x2F
REG_MOD_READ = 0x80
# REG_MOD_MULTI = 0x40

LIS3DH_DEV_ID = 0x33

FREEFALL_ACCEL = 9.80665
# /16 => The sampling accuracy of lis3dh is 12 valid bits
SCALE = FREEFALL_ACCEL * 1000 * (8*2)/16 * 1 / 2**12
BYTES_PER_SAMPLE = 6
SAMPLES_PER_BLOCK = bulk_sensor.MAX_BULK_MSG_SIZE // BYTES_PER_SAMPLE

BATCH_UPDATES = 0.100

# Printer class that controls LIS3DH chip
class LIS3DH:
    def __init__(self, config):
        self.printer = config.get_printer()
        adxl345.AccelCommandHelper(config, self)
        self.axes_map = self.read_axes_map(config)
        self.data_rate = 1344
        # Setup mcu sensor_lis3dh bulk query code
        self.spi = bus.MCU_SPI_from_config(config, 3, default_speed=5000000)
        self.mcu = mcu = self.spi.get_mcu()
        self.oid = oid = mcu.create_oid()
        self.query_lis3dh_cmd = None
        mcu.add_config_cmd("config_lis3dh oid=%d spi_oid=%d"
                           % (oid, self.spi.get_oid()))
        mcu.add_config_cmd("query_lis3dh oid=%d rest_ticks=0"
                           % (oid,), on_restart=True)
        mcu.register_config_callback(self._build_config)
        self.bulk_queue = bulk_sensor.BulkDataQueue(mcu, oid=oid)
        # Clock tracking
        chip_smooth = self.data_rate * BATCH_UPDATES * 2
        self.clock_sync = bulk_sensor.ClockSyncRegression(mcu, chip_smooth)
        self.clock_updater = bulk_sensor.ChipClockUpdater(self.clock_sync,
                                                          BYTES_PER_SAMPLE)
        self.last_error_count = 0
        # Process messages in batches
        self.batch_bulk = bulk_sensor.BatchBulkHelper(
            self.printer, self._process_batch,
            self._start_measurements, self._finish_measurements, BATCH_UPDATES)
        self.name = config.get_name().split()[-1]
        hdr = ('time', 'x_acceleration', 'y_acceleration', 'z_acceleration')
        self.batch_bulk.add_mux_endpoint("lis3dh/dump_lis3dh", "sensor",
                                         self.name, {'header': hdr})
    def read_axes_map(self,config):
        am = {'x': (0, SCALE), 'y': (1, SCALE), 'z': (2, SCALE),
            '-x': (0, 0), '-y': (1, 0), '-z': (2, 0)}
        axes_map = config.getlist('axes_map', ('x','y','z'), count=3)
        if any([a not in am for a in axes_map]):
            raise config.error("Invalid axes_map parameter")
        return [am[a.strip()] for a in axes_map]
    def _build_config(self):
        cmdqueue = self.spi.get_command_queue()
        self.query_lis3dh_cmd = self.mcu.lookup_command(
            "query_lis3dh oid=%c rest_ticks=%u", cq=cmdqueue)
        self.clock_updater.setup_query_command(
            self.mcu, "query_lis3dh_status oid=%c", oid=self.oid, cq=cmdqueue)
    def read_reg(self, reg):
        params = self.spi.spi_transfer([reg | REG_MOD_READ, 0x00])
        response = bytearray(params['response'])
        return response[1]
    def read_regs(self, reg,size):
        send_trans = [reg | REG_MOD_READ]
        for i in range(size):
            send_trans.append(0x00)
        params = self.spi.spi_transfer(send_trans)
        response = bytearray(params['response'])
        return response[1:]
    def set_reg(self, reg, val, minclock=0):
        self.spi.spi_send([reg, val & 0xFF], minclock=minclock)
        stored_val = self.read_reg(reg)
        if stored_val != val:
            raise self.printer.command_error(
                    "Failed to set LIS3DH register [0x%x] to 0x%x: got 0x%x. "
                    "This is generally indicative of connection problems "
                    "(e.g. faulty wiring) or a faulty lis3dh chip." % (
                        reg, val, stored_val))
    def start_internal_client(self):
        aqh = adxl345.AccelQueryHelper(self.printer)
        self.batch_bulk.add_client(aqh.handle_batch)
        return aqh
    # Measurement decoding
    def _extract_samples(self, raw_samples):
        # Load variables to optimize inner loop below
        (x_pos, x_scale), (y_pos, y_scale), (z_pos, z_scale) = self.axes_map
        logging.info("self.axes_map:",str(self.axes_map))
        last_sequence = self.clock_updater.get_last_sequence()
        time_base, chip_base, inv_freq = self.clock_sync.get_time_translation()
        # Process every message in raw_samples
        count = seq = 0
        samples = [None] * (len(raw_samples) * SAMPLES_PER_BLOCK)
        for params in raw_samples:
            seq_diff = (params['sequence'] - last_sequence) & 0xffff
            seq_diff -= (seq_diff & 0x8000) << 1
            seq = last_sequence + seq_diff
            d = bytearray(params['data'])
            msg_cdiff = seq * SAMPLES_PER_BLOCK - chip_base

            for i in range(len(d) // BYTES_PER_SAMPLE):
                d_xyz = d[i*BYTES_PER_SAMPLE:(i+1)*BYTES_PER_SAMPLE]
                xlow, xhigh, ylow, yhigh, zlow, zhigh = d_xyz
                # Merge and perform twos-complement

                rx = (((xhigh << 8) | xlow)) - ((xhigh & 0x80) << 9)
                ry = (((yhigh << 8) | ylow)) - ((yhigh & 0x80) << 9)
                rz = (((zhigh << 8) | zlow)) - ((zhigh & 0x80) << 9)
                raw_xyz = (rx, ry, rz)

                x = round(raw_xyz[x_pos] * x_scale, 6)
                y = round(raw_xyz[y_pos] * y_scale, 6)
                z = round(raw_xyz[z_pos] * z_scale, 6)

                ptime = round(time_base + (msg_cdiff + i) * inv_freq, 6)
                samples[count] = (ptime, x, y, z)
                count += 1
        self.clock_sync.set_last_chip_clock(seq * SAMPLES_PER_BLOCK + i)
        del samples[count:]
        return samples
    # Start, stop, and process message batches
    def _start_measurements(self):
        # In case of miswiring, testing LIS3DH device ID prevents treating
        # noise or wrong signal as a correctly initialized device
        dev_id = self.read_reg(REG_LIS3DH_WHO_AM_I_ADDR)
        logging.info("lis3dh_dev_id: %x", dev_id)
        if dev_id != LIS3DH_DEV_ID:
            raise self.printer.command_error(
                "Invalid lis3dh id (got %x vs %x).\n"
                "This is generally indicative of connection problems\n"
                "(e.g. faulty wiring) or a faulty lis3dh chip."
                % (dev_id, LIS3DH_DEV_ID))
        self.set_reg(REG_LIS3DH_CTRL_REG1_ADDR, 0x97)
        self.set_reg(REG_LIS3DH_CTRL_REG2_ADDR, 0)
        self.set_reg(REG_LIS3DH_CTRL_REG3_ADDR, 0)
        self.set_reg(REG_LIS3DH_CTRL_REG4_ADDR, 0x28)
        self.set_reg(REG_LIS3DH_CTRL_REG5_ADDR, 0x40)
        self.set_reg(REG_LIS3DH_CTRL_REG6_ADDR, 0)
        self.set_reg(REG_LIS3DH_FIFO_CTRL, 0)
        self.set_reg(REG_LIS3DH_FIFO_CTRL, 0x80)
        # Start bulk reading
        self.bulk_queue.clear_samples()
        rest_ticks = self.mcu.seconds_to_clock(4. / (self.data_rate))
        self.query_lis3dh_cmd.send([self.oid, rest_ticks])
        self.set_reg(REG_LIS3DH_FIFO_CTRL, 0x80)
        logging.info("LIS3DH starting '%s' measurements", self.name)
        # Initialize clock tracking
        self.clock_updater.note_start()
        self.last_error_count = 0
    def _finish_measurements(self):
        # Halt bulk reading
        self.set_reg(REG_LIS3DH_FIFO_CTRL, 0x00)
        self.query_lis3dh_cmd.send_wait_ack([self.oid, 0])
        self.bulk_queue.clear_samples()
        logging.info("LIS3DH finished '%s' measurements", self.name)
        self.set_reg(REG_LIS3DH_FIFO_CTRL, 0x00)
        self.set_reg(REG_LIS3DH_CTRL_REG1_ADDR, 0)
    def _process_batch(self, eventtime):
        self.clock_updater.update_clock()
        raw_samples = self.bulk_queue.pull_samples()
        if not raw_samples:
            return {}
        samples = self._extract_samples(raw_samples)
        if not samples:
            return {}
        return {'data': samples, 'errors': self.last_error_count,
                'overflows': self.clock_updater.get_last_overflows()}

def load_config(config):
    return LIS3DH(config)

def load_config_prefix(config):
    return LIS3DH(config)
