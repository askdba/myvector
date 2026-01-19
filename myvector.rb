class Myvector < Formula
  desc "MyVector: Vector Storage & Search Plugin for MySQL"
  homepage "https://github.com/askdba/myvector"
  url "https://github.com/askdba/myvector/archive/f8727c2458628f0b308e292e77f59083b6872600.tar.gz"
  version "0.1.0"
  sha256 "1b0d943736372dc8f4de79e44116a54731712a59a296914141197d0f12aff675"
  license "GPL-2.0-only"

  depends_on "cmake" => :build
  depends_on "mysql"

  def install
    mkdir "build" do
      system "cmake", "..", *std_cmake_args
      system "make", "install"
    end
  end

  test do
    (testpath/"test.sql").write <<~EOS
      SELECT myvector_is_valid(myvector_construct("[1,2,3]"), 3);
    EOS
    assert_match "1", shell_output("mysql -u root -p < test.sql")
  end
end
